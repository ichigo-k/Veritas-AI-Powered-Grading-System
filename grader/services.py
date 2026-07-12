"""
GraderService — core orchestration for AI-powered grading.

Coordinates plagiarism detection, concurrent attempt grading via a thread pool,
Bedrock invocation per answer, score computation, and database persistence.

# Feature: verion-ai-grader, Property 4: Bedrock failure for one answer does not prevent others
"""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from django.conf import settings
from django.http import Http404

from grader.bedrock import BedrockClient
from grader.gemini import GeminiClient
from grader.ollama import OllamaClient
from grader.exceptions import GradingError
from grader.types import CriterionScore, GradeResponse, FileAttachment
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


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AnswerFeedbackResult:
    """Grading result for a single subjective answer."""

    question_id: int
    total_score: float
    max_score: float
    flag: str
    flag_reason: str
    criteria_feedback: list[dict]
    bedrock_error: bool = False


@dataclass
class SingleGradingResult:
    """Grading result for a single attempt."""

    attempt_id: int
    score: float
    plagiarism_flagged: bool
    answer_feedbacks: list[AnswerFeedbackResult] = field(default_factory=list)
    error_notes: str = ""


@dataclass
class BatchGradingResult:
    """Summary result for a batch (assessment-level) grading run."""

    assessment_id: int
    graded_count: int
    grading_status: str
    plagiarism_flags: list[dict] = field(default_factory=list)
    # Each entry: {"question_id": int, "flagged_attempt_ids": list[int]}


# ---------------------------------------------------------------------------
# GraderService
# ---------------------------------------------------------------------------


class GraderService:
    """
    Orchestrates grading for assessments and individual attempts.

    Reads configuration from django.conf.settings:
        BEDROCK_MODEL_ID, BEDROCK_MAX_TOKENS, AWS_REGION, GRADING_CONCURRENCY
    """

    def __init__(self) -> None:
        if settings.AI_PROVIDER == "ollama":
            self._ai_client = OllamaClient(settings.OLLAMA_BASE_URL, settings.OLLAMA_MODEL_ID, settings.BEDROCK_MAX_TOKENS, settings.OLLAMA_TIMEOUT, settings.OLLAMA_NUM_CTX)
        elif settings.AI_PROVIDER == "gemini":
            self._ai_client = GeminiClient(settings.GEMINI_API_KEY, settings.GEMINI_MODEL_ID, settings.BEDROCK_MAX_TOKENS, settings.GEMINI_TIMEOUT)
        else:
            self._ai_client = BedrockClient(settings.BEDROCK_MODEL_ID, settings.BEDROCK_MAX_TOKENS, settings.AWS_REGION, settings.AWS_ACCESS_KEY_ID, settings.AWS_SECRET_ACCESS_KEY)
        # S3 is optional: only build the helper when a bucket is configured.
        # Without it, answers with file attachments are flagged per-answer
        # rather than crashing the service (relevant for Ollama-only setups).
        storage_config = None
        if storage_config:
            self._s3_helper = S3Helper(
                bucket_name=storage_config.bucket_name,
                region=storage_config.region,
                upload_prefix=storage_config.upload_prefix,
                presigned_url_expires_in=storage_config.presigned_url_expires_in,
                endpoint_url=storage_config.endpoint_url,
                aws_access_key_id=storage_config.aws_access_key_id,
                aws_secret_access_key=storage_config.aws_secret_access_key,
            )
        else:
            self._s3_helper = None

    # ---------------------------------------------------------------------------
    # Task 9.3 — Batch grading
    # ---------------------------------------------------------------------------

    def grade_assessment(self, assessment_id: int) -> BatchGradingResult:
        """
        Grade all eligible attempts for an assessment concurrently.

        Eligible attempts have status 'SUBMITTED' or 'TIMED_OUT'.
        Plagiarism detection is performed across all answers before grading begins.
        After all attempts are graded, assessments.gradingStatus is set to 'GRADED'.

        Args:
            assessment_id: Primary key of the Assessment to grade.

        Returns:
            BatchGradingResult with graded_count, grading_status, and plagiarism_flags.

        Raises:
            Http404: If no Assessment row exists for assessment_id.
        """
        # Step 1: Fetch assessment — 404 if not found
        try:
            assessment = Assessment.objects.get(id=assessment_id)
        except Assessment.DoesNotExist:
            raise Http404(f"Assessment {assessment_id} not found.")

        # Step 2: Fetch eligible attempts
        attempts = list(
            AssessmentAttempt.objects.filter(
                assessment_id=assessment_id,
                status__in=("SUBMITTED", "TIMED_OUT"),
            )
        )

        # Step 3: No eligible attempts — return early
        if not attempts:
            return BatchGradingResult(
                assessment_id=assessment_id,
                graded_count=0,
                grading_status="NOT_GRADED",
            )

        attempt_ids = [a.id for a in attempts]

        # Step 4: Fetch all student answers for those attempts in one query
        all_answers = list(
            StudentAnswer.objects.filter(attempt_id__in=attempt_ids)
        )

        # Step 5: Build plagiarism collision map
        scanner = PlagiarismScanner()
        collision_map = scanner.build_collision_map(all_answers)

        # Step 6: Build plagiarism_flags list for the response
        plagiarism_flags: list[dict] = []
        for (q_id, _hash), flagged_ids in collision_map.items():
            if len(flagged_ids) > 1:
                plagiarism_flags.append(
                    {
                        "question_id": q_id,
                        "flagged_attempt_ids": flagged_ids,
                    }
                )

        # Step 7: Get the flat set of flagged attempt IDs
        flagged_attempt_ids = scanner.get_flagged_attempts(collision_map)

        # Step 8 & 9: Grade all attempts concurrently
        results: list[SingleGradingResult] = []
        for attempt in attempts:
            results.append(self._grade_single_attempt_worker(attempt, flagged_attempt_ids, assessment.total_marks))

        # Step 10: Mark assessment as GRADED
        Assessment.objects.filter(id=assessment_id).update(grading_status="GRADED")

        # Step 11: Return summary
        return BatchGradingResult(
            assessment_id=assessment_id,
            graded_count=len(results),
            grading_status="GRADED",
            plagiarism_flags=plagiarism_flags,
        )

    # ---------------------------------------------------------------------------
    # Task 9.4 — Single attempt grading
    # ---------------------------------------------------------------------------

    def grade_attempt(self, attempt_id: int) -> SingleGradingResult:
        """
        Grade a single attempt, including plagiarism check against other attempts.

        Args:
            attempt_id: Primary key of the AssessmentAttempt to grade.

        Returns:
            SingleGradingResult with score, grade, plagiarism flag, and per-answer feedback.

        Raises:
            Http404: If no AssessmentAttempt row exists for attempt_id.
        """
        # Step 1: Fetch attempt — 404 if not found
        try:
            attempt = AssessmentAttempt.objects.get(id=attempt_id)
        except AssessmentAttempt.DoesNotExist:
            raise Http404(f"Attempt {attempt_id} not found.")

        # Step 2: Fetch all answers for this attempt
        this_attempt_answers = list(
            StudentAnswer.objects.filter(attempt_id=attempt_id)
        )

        # Step 3: Fetch all other answers for the same assessment for plagiarism check
        other_attempt_ids = list(
            AssessmentAttempt.objects.filter(
                assessment_id=attempt.assessment_id,
            )
            .exclude(id=attempt_id)
            .values_list("id", flat=True)
        )
        other_answers = list(
            StudentAnswer.objects.filter(attempt_id__in=other_attempt_ids)
        )

        # Step 4: Build plagiarism map across all answers (this attempt + others)
        all_answers = this_attempt_answers + other_answers
        scanner = PlagiarismScanner()
        collision_map = scanner.build_collision_map(all_answers)

        # Step 5: Get flagged set; check if this attempt is in it
        flagged_attempt_ids = scanner.get_flagged_attempts(collision_map)

        # Step 6: Fetch assessment for total_marks
        try:
            assessment = Assessment.objects.get(id=attempt.assessment_id)
        except Assessment.DoesNotExist:
            raise Http404(f"Assessment {attempt.assessment_id} not found.")

        # Step 7 & 8: Grade the attempt and return the result
        return self._grade_single_attempt_worker(
            attempt,
            flagged_attempt_ids,
            assessment.total_marks,
        )

    # ---------------------------------------------------------------------------
    # Task 9.2 — Per-attempt worker (runs in thread pool or directly)
    # ---------------------------------------------------------------------------

    def _grade_single_attempt_worker(
        self,
        attempt: AssessmentAttempt,
        flagged_attempts: set[int],
        assessment_total_marks: int,
    ) -> SingleGradingResult:
        """
        Grade all subjective answers for a single attempt.

        Handles BedrockGradingError per-answer (score=0, bedrock_error=True, continue).
        Wraps the entire body in a try/except so a catastrophic failure still
        produces a SingleGradingResult with score=0 and grade='F'.

        Args:
            attempt: The AssessmentAttempt to grade.
            flagged_attempts: Set of attempt IDs flagged for plagiarism.
            assessment_total_marks: Total marks for the assessment.

        Returns:
            SingleGradingResult with all per-answer feedback populated.
        """
        try:
            return self._do_grade_attempt(attempt, flagged_attempts, assessment_total_marks)
        except Exception:
            logger.exception(
                "Unhandled error grading attempt %d; recording error and returning score=0",
                attempt.id,
            )
            error_msg = f"Unhandled error during grading of attempt {attempt.id}."
            # Persist an error GradingResult so the failure is auditable
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
                logger.exception(
                    "Failed to persist error GradingResult for attempt %d", attempt.id
                )
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
        """
        Inner implementation of attempt grading (no top-level exception handling).

        Separated from _grade_single_attempt_worker so the outer method can
        cleanly catch all exceptions without masking the logic here.
        """
        # Step 1: Fetch subjective answers for this attempt
        subjective_section_ids = list(
            AssessmentSection.objects.filter(
                assessment_id=attempt.assessment_id,
                type="SUBJECTIVE",
            ).values_list("id", flat=True)
        )
        subjective_question_ids = list(
            Question.objects.filter(
                assessment_id=attempt.assessment_id,
                section_id__in=subjective_section_ids,
            ).values_list("id", flat=True)
        )
        answers = list(
            StudentAnswer.objects.filter(
                attempt_id=attempt.id,
                question_id__in=subjective_question_ids,
            )
        )

        # Step 2: Grade each answer
        feedbacks: list[AnswerFeedbackResult] = []
        error_notes_parts: list[str] = []

        for answer in answers:
            # Step 2a: Fetch question for marks
            try:
                question = Question.objects.get(id=answer.question_id)
            except Question.DoesNotExist:
                logger.error(
                    "Question %d not found for answer %d (attempt %d); skipping.",
                    answer.question_id,
                    answer.id,
                    attempt.id,
                )
                continue

            # Step 2b: Fetch rubric criteria for this question
            rubric_criteria_qs = list(
                RubricCriterion.objects.filter(
                    question_id=question.id,
                ).order_by("order")
            )
            rubric_list = [
                {"description": rc.description, "max_marks": rc.max_marks}
                for rc in rubric_criteria_qs
            ]

            # Step 2c: Call Bedrock
            try:
                attachment = None
                if answer.file_url:
                    if self._s3_helper is None:
                        raise S3ResolutionError(
                            "Answer has a file attachment but S3 is not configured "
                            "(set S3_BUCKET_NAME to enable attachment grading)."
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
                # Step 2d: Grading error — score=0, bedrock_error=True, continue
                logger.error(
                    "GradingError for attempt %d, question %d: %s",
                    attempt.id,
                    question.id,
                    exc,
                )
                error_notes_parts.append(
                    f"AI grading error for question {question.id}: {exc}"
                )
                feedbacks.append(
                    AnswerFeedbackResult(
                        question_id=question.id,
                        total_score=0.0,
                        max_score=float(question.marks),
                        flag="",
                        flag_reason="",
                        criteria_feedback=[],
                        bedrock_error=True,
                    )
                )
                continue

            # Step 2e: Cap scores and build criteria_feedback list
            if grade_response.criteria_scores:
                # Rubric-guided grading
                capped_criteria: list[dict] = []
                answer_total = 0.0
                for cs in grade_response.criteria_scores:
                    capped = cap_criterion_score(cs.awarded, cs.max)
                    answer_total += capped
                    capped_criteria.append(
                        {
                            "criterion": cs.criterion,
                            "awarded": capped,
                            "max": cs.max,
                            "justification": cs.justification,
                        }
                    )
                # Cap total at question.marks
                answer_total = min(answer_total, float(question.marks))
                criteria_feedback = capped_criteria
            else:
                # Holistic grading
                holistic = grade_response.holistic_score or 0
                answer_total = float(min(holistic, question.marks))
                criteria_feedback = []

            # Step 2f: Build AnswerFeedbackResult
            feedbacks.append(
                AnswerFeedbackResult(
                    question_id=question.id,
                    total_score=answer_total,
                    max_score=float(question.marks),
                    flag="" if grade_response.flag == "none" else grade_response.flag,
                    flag_reason=grade_response.flag_reason,
                    criteria_feedback=criteria_feedback,
                    bedrock_error=False,
                )
            )

        # Step 3 & 4: Compute scores
        existing_mcq_score: float = attempt.score or 0.0
        subjective_score_list = [f.total_score for f in feedbacks]

        # Step 5: Compute final score
        final_score = compute_final_score(
            existing_mcq_score,
            subjective_score_list,
            assessment_total_marks,
        )

        # Step 6: Check plagiarism flag
        plagiarism_flagged = attempt.id in flagged_attempts

        # Step 8: Update attempt score only (grade is computed on read by the main system)
        AssessmentAttempt.objects.filter(id=attempt.id).update(
            score=final_score,
        )

        # Step 9: Persist GradingResult
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

        # Step 10: Replace AnswerFeedback rows
        AnswerFeedback.objects.filter(grading_result=grading_result).delete()
        AnswerFeedback.objects.bulk_create(
            [
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
            ]
        )

        # Step 11: Return result
        return SingleGradingResult(
            attempt_id=attempt.id,
            score=final_score,
            plagiarism_flagged=plagiarism_flagged,
            answer_feedbacks=feedbacks,
            error_notes=error_notes,
        )


