import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone

from django.conf import settings
from django.http import Http404

from grader.bedrock import BedrockClient
from grader.gemini import GeminiClient
from grader.ollama import OllamaClient
from grader.exceptions import GradingError
from grader.types import GradeResponse, FileAttachment
from grader.models import (
    AnswerFeedback,
    Assessment,
    AssessmentAttempt,
    AssessmentSection,
    GradingResult,
    Question,
    RubricCriterion,
    StudentAnswer,
)
from grader.plagiarism import PlagiarismScanner
from grader.scoring import cap_criterion_score, compute_final_score
from grader.s3 import S3Helper, S3ResolutionError

logger = logging.getLogger(__name__)


def latest_eligible_attempts(assessment_id: int) -> list[AssessmentAttempt]:
    """Return only the latest submitted/timed-out attempt for each student."""
    ordered = AssessmentAttempt.objects.filter(
        assessment_id=assessment_id,
        status__in=("SUBMITTED", "TIMED_OUT"),
    ).order_by("student_id", "-attempt_number", "-id")
    latest: list[AssessmentAttempt] = []
    seen_students: set[int] = set()
    for attempt in ordered:
        if attempt.student_id in seen_students:
            continue
        seen_students.add(attempt.student_id)
        latest.append(attempt)
    return latest


@dataclass
class AnswerFeedbackResult:
    question_id: int
    total_score: float
    max_score: float
    flag: str
    flag_reason: str
    criteria_feedback: list[dict]
    bedrock_error: bool = False


@dataclass
class SingleGradingResult:
    attempt_id: int
    score: float
    plagiarism_flagged: bool
    answer_feedbacks: list[AnswerFeedbackResult] = field(default_factory=list)
    error_notes: str = ""


@dataclass
class BatchGradingResult:
    assessment_id: int
    graded_count: int
    grading_status: str
    plagiarism_flags: list[dict] = field(default_factory=list)


class GraderService:

    def __init__(self) -> None:
        if settings.AI_PROVIDER == "ollama":
            self._ai_client = OllamaClient(
                settings.OLLAMA_BASE_URL, settings.OLLAMA_MODEL_ID,
                settings.BEDROCK_MAX_TOKENS, settings.OLLAMA_TIMEOUT, settings.OLLAMA_NUM_CTX,
            )
        elif settings.AI_PROVIDER == "gemini":
            self._ai_client = GeminiClient(
                settings.GEMINI_API_KEY, settings.GEMINI_MODEL_ID,
                settings.BEDROCK_MAX_TOKENS, settings.GEMINI_TIMEOUT,
            )
        else:
            self._ai_client = BedrockClient(
                settings.BEDROCK_MODEL_ID, settings.BEDROCK_MAX_TOKENS,
                settings.AWS_REGION, settings.AWS_ACCESS_KEY_ID,
                settings.AWS_SECRET_ACCESS_KEY, settings.BEDROCK_REQUEST_DELAY,
            )
        self._s3_helper = None

    def grade_assessment(self, assessment_id: int) -> BatchGradingResult:
        try:
            assessment = Assessment.objects.get(id=assessment_id)
        except Assessment.DoesNotExist:
            raise Http404(f"Assessment {assessment_id} not found.")

        attempts = latest_eligible_attempts(assessment_id)

        # The portal cancels through the shared assessment row. A job that was
        # cancelled while waiting in SQS must not start later.
        assessment.refresh_from_db(fields=["grading_status"])
        if assessment.grading_status != "GRADING":
            logger.info("[grade_assessment] cancelled before start: assessment_id=%d", assessment_id)
            return BatchGradingResult(
                assessment_id=assessment_id,
                graded_count=0,
                grading_status=assessment.grading_status,
            )

        unique_students = len({a.student_id for a in attempts})
        logger.info(
            "[grade_assessment] started: assessment_id=%d total_attempts=%d unique_students=%d provider=%s concurrency=%d",
            assessment_id, len(attempts), unique_students,
            settings.AI_PROVIDER, settings.GRADING_CONCURRENCY,
        )

        if not attempts:
            logger.info("[grade_assessment] no eligible attempts, skipping: assessment_id=%d", assessment_id)
            return BatchGradingResult(
                assessment_id=assessment_id,
                graded_count=0,
                grading_status="NOT_GRADED",
            )

        attempt_ids = [a.id for a in attempts]

        all_answers = list(StudentAnswer.objects.filter(attempt_id__in=attempt_ids))
        logger.info("[grade_assessment] loaded %d answers for plagiarism scan", len(all_answers))

        scanner = PlagiarismScanner()
        collision_map = scanner.build_collision_map(all_answers)
        flagged_attempt_ids = scanner.get_flagged_attempts(collision_map)

        plagiarism_flags: list[dict] = []
        for (q_id, _hash), flagged_ids in collision_map.items():
            if len(flagged_ids) > 1:
                plagiarism_flags.append({"question_id": q_id, "flagged_attempt_ids": flagged_ids})

        if flagged_attempt_ids:
            logger.info("[grade_assessment] plagiarism: %d attempts flagged across %d collision groups",
                        len(flagged_attempt_ids), len(plagiarism_flags))

        results: list[SingleGradingResult] = []
        concurrency = max(1, settings.GRADING_CONCURRENCY)

        logger.info("[grade_assessment] grading %d attempts with concurrency=%d", len(attempts), concurrency)

        if concurrency == 1:
            for i, attempt in enumerate(attempts, 1):
                assessment.refresh_from_db(fields=["grading_status"])
                if assessment.grading_status != "GRADING":
                    logger.info("[grade_assessment] cancellation observed: assessment_id=%d", assessment_id)
                    break
                logger.info("[grade_assessment] grading attempt %d/%d attempt_id=%d", i, len(attempts), attempt.id)
                results.append(
                    self._grade_single_attempt_worker(attempt, flagged_attempt_ids, assessment.total_marks)
                )
                logger.info("[grade_assessment] finished attempt %d/%d attempt_id=%d", i, len(attempts), attempt.id)
        else:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(
                        self._grade_single_attempt_worker, attempt, flagged_attempt_ids, assessment.total_marks
                    ): attempt
                    for attempt in attempts
                }
                done_count = 0
                for future in as_completed(futures):
                    done_count += 1
                    attempt = futures[future]
                    try:
                        result = future.result()
                        results.append(result)
                        logger.info(
                            "[grade_assessment] attempt done %d/%d attempt_id=%d score=%.1f",
                            done_count, len(attempts), attempt.id, result.score,
                        )
                    except Exception:
                        logger.exception(
                            "[grade_assessment] unexpected future error attempt_id=%d", attempt.id
                        )

        total_questions = sum(len(r.answer_feedbacks) for r in results)
        failed_questions = sum(1 for r in results for fb in r.answer_feedbacks if fb.bedrock_error)

        logger.info(
            "[grade_assessment] complete: assessment_id=%d graded=%d questions_total=%d passed=%d failed=%d",
            assessment_id, len(results), total_questions,
            total_questions - failed_questions, failed_questions,
        )

        assessment.refresh_from_db(fields=["grading_status"])
        if assessment.grading_status != "GRADING":
            logger.info("[grade_assessment] cancelled; leaving partial progress: assessment_id=%d", assessment_id)
            return BatchGradingResult(
                assessment_id=assessment_id,
                graded_count=len(results),
                grading_status=assessment.grading_status,
                plagiarism_flags=plagiarism_flags,
            )

        Assessment.objects.filter(id=assessment_id).update(grading_status="GRADED")
        logger.info("[grade_assessment] marked GRADED: assessment_id=%d", assessment_id)

        return BatchGradingResult(
            assessment_id=assessment_id,
            graded_count=len(results),
            grading_status="GRADED",
            plagiarism_flags=plagiarism_flags,
        )

    def grade_attempt(self, attempt_id: int) -> SingleGradingResult:
        try:
            attempt = AssessmentAttempt.objects.get(id=attempt_id)
        except AssessmentAttempt.DoesNotExist:
            raise Http404(f"Attempt {attempt_id} not found.")

        this_attempt_answers = list(StudentAnswer.objects.filter(attempt_id=attempt_id))

        other_attempt_ids = list(
            AssessmentAttempt.objects.filter(assessment_id=attempt.assessment_id)
            .exclude(id=attempt_id)
            .values_list("id", flat=True)
        )
        other_answers = list(StudentAnswer.objects.filter(attempt_id__in=other_attempt_ids))

        all_answers = this_attempt_answers + other_answers
        scanner = PlagiarismScanner()
        collision_map = scanner.build_collision_map(all_answers)
        flagged_attempt_ids = scanner.get_flagged_attempts(collision_map)

        try:
            assessment = Assessment.objects.get(id=attempt.assessment_id)
        except Assessment.DoesNotExist:
            raise Http404(f"Assessment {attempt.assessment_id} not found.")

        return self._grade_single_attempt_worker(attempt, flagged_attempt_ids, assessment.total_marks)

    def _grade_single_attempt_worker(
        self,
        attempt: AssessmentAttempt,
        flagged_attempts: set[int],
        assessment_total_marks: int,
    ) -> SingleGradingResult:
        try:
            return self._do_grade_attempt(attempt, flagged_attempts, assessment_total_marks)
        except Exception:
            logger.exception("[_grade_single_attempt_worker] unhandled error attempt_id=%d", attempt.id)
            error_msg = f"Unhandled error during grading of attempt {attempt.id}."
            try:
                GradingResult.objects.update_or_create(
                    attempt_id=attempt.id,
                    defaults={
                        "assessment_id": attempt.assessment_id,
                        "score": 0.0,
                        "plagiarism_flagged": attempt.id in flagged_attempts,
                        "graded_at": datetime.now(tz=timezone.utc),
                        "error_notes": error_msg,
                    },
                )
            except Exception:
                logger.exception("[_grade_single_attempt_worker] failed to persist error result attempt_id=%d", attempt.id)
            return SingleGradingResult(
                attempt_id=attempt.id,
                score=0.0,
                plagiarism_flagged=attempt.id in flagged_attempts,
                error_notes=error_msg,
            )

    def _do_grade_attempt(
        self,
        attempt: AssessmentAttempt,
        flagged_attempts: set[int],
        assessment_total_marks: int,
    ) -> SingleGradingResult:
        subjective_section_ids = list(
            AssessmentSection.objects.filter(
                assessment_id=attempt.assessment_id, type="SUBJECTIVE",
            ).values_list("id", flat=True)
        )
        subjective_question_ids = list(
            Question.objects.filter(
                assessment_id=attempt.assessment_id,
                section_id__in=subjective_section_ids,
            ).values_list("id", flat=True)
        )
        answers = [
            answer
            for answer in StudentAnswer.objects.filter(
                attempt_id=attempt.id,
                question_id__in=subjective_question_ids,
            )
            if (answer.answer_text or "").strip() or answer.file_url
        ]

        feedbacks: list[AnswerFeedbackResult] = []
        error_notes_parts: list[str] = []

        for answer in answers:
            try:
                question = Question.objects.get(id=answer.question_id)
            except Question.DoesNotExist:
                logger.error("[_do_grade_attempt] question %d not found, skipping", answer.question_id)
                continue

            rubric_criteria_qs = list(
                RubricCriterion.objects.filter(question_id=question.id).order_by("order")
            )
            rubric_list = [
                {"description": rc.description, "max_marks": rc.max_marks}
                for rc in rubric_criteria_qs
            ]

            try:
                attachment = None
                if answer.file_url:
                    if self._s3_helper is None:
                        raise S3ResolutionError(
                            "Answer has a file attachment but S3 is not configured."
                        )
                    resolved_file = self._s3_helper.resolve_object(answer.file_url)
                    attachment = FileAttachment(
                        media_type=resolved_file.content_type,
                        data=resolved_file.body,
                        filename=resolved_file.filename,
                    )

                grade_response = self._ai_client.grade_answer(
                    question_body=question.body,
                    answer_text=answer.answer_text or "",
                    rubric_criteria=rubric_list,
                    question_marks=question.marks,
                    attachment=attachment,
                )
            except (GradingError, S3ResolutionError) as exc:
                logger.error(
                    "[_do_grade_attempt] grading error attempt_id=%d question_id=%d: %s",
                    attempt.id, question.id, exc,
                )
                error_notes_parts.append(f"AI grading error for question {question.id}: {exc}")
                feedbacks.append(AnswerFeedbackResult(
                    question_id=question.id, total_score=0.0,
                    max_score=float(question.marks), flag="", flag_reason="",
                    criteria_feedback=[], bedrock_error=True,
                ))
                continue

            if grade_response.criteria_scores:
                capped_criteria: list[dict] = []
                answer_total = 0.0
                for cs in grade_response.criteria_scores:
                    capped = cap_criterion_score(cs.awarded, cs.max)
                    answer_total += capped
                    capped_criteria.append({
                        "criterion": cs.criterion, "awarded": capped,
                        "max": cs.max, "justification": cs.justification,
                    })
                answer_total = min(answer_total, float(question.marks))
                criteria_feedback = capped_criteria
            else:
                holistic = grade_response.holistic_score or 0
                answer_total = float(min(holistic, question.marks))
                criteria_feedback = []

            feedbacks.append(AnswerFeedbackResult(
                question_id=question.id,
                total_score=answer_total,
                max_score=float(question.marks),
                flag="" if grade_response.flag == "none" else grade_response.flag,
                flag_reason=grade_response.flag_reason,
                criteria_feedback=criteria_feedback,
                bedrock_error=False,
            ))

        # `attempt.score` is the final score after the first grading run. Reusing
        # it here as the MCQ subtotal makes every re-grade add the subjective
        # marks again (and often cap at the assessment maximum). Rebuild the
        # objective subtotal from the answers so grading is idempotent.
        objective_section_ids = AssessmentSection.objects.filter(
            assessment_id=attempt.assessment_id,
            type="OBJECTIVE",
        ).values_list("id", flat=True)
        objective_questions = {
            question.id: question
            for question in Question.objects.filter(
                assessment_id=attempt.assessment_id,
                section_id__in=objective_section_ids,
                correct_option__isnull=False,
            )
        }
        mcq_score = sum(
            float(objective_questions[answer.question_id].marks)
            for answer in StudentAnswer.objects.filter(
                attempt_id=attempt.id,
                question_id__in=objective_questions,
            )
            if answer.selected_option
            == objective_questions[answer.question_id].correct_option
        )
        final_score = compute_final_score(
            mcq_score,
            [f.total_score for f in feedbacks],
            assessment_total_marks,
        )
        plagiarism_flagged = attempt.id in flagged_attempts

        AssessmentAttempt.objects.filter(id=attempt.id).update(score=final_score)

        error_notes = "; ".join(error_notes_parts)
        grading_result, _ = GradingResult.objects.update_or_create(
            attempt_id=attempt.id,
            defaults={
                "assessment_id": attempt.assessment_id,
                "score": final_score,
                "plagiarism_flagged": plagiarism_flagged,
                "graded_at": datetime.now(tz=timezone.utc),
                "error_notes": error_notes,
            },
        )

        AnswerFeedback.objects.filter(grading_result=grading_result).delete()
        AnswerFeedback.objects.bulk_create([
            AnswerFeedback(
                grading_result=grading_result,
                question_id=fb.question_id,
                total_score=fb.total_score,
                max_score=fb.max_score,
                flag=fb.flag,
                flag_reason=fb.flag_reason,
                criteria_feedback=fb.criteria_feedback,
                bedrock_error=fb.bedrock_error,
            )
            for fb in feedbacks
        ])

        logger.info(
            "[_do_grade_attempt] done: attempt_id=%d student_id=%d score=%.1f questions=%d errors=%d",
            attempt.id, attempt.student_id, final_score, len(feedbacks),
            sum(1 for fb in feedbacks if fb.bedrock_error),
        )

        return SingleGradingResult(
            attempt_id=attempt.id,
            score=final_score,
            plagiarism_flagged=plagiarism_flagged,
            answer_feedbacks=feedbacks,
            error_notes=error_notes,
        )
