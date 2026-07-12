"""
AWS Bedrock client for AI-powered answer grading.

Wraps boto3's bedrock-runtime client to invoke a configured model with
structured prompts and parse the JSON response into GradeResponse objects.

Retry logic: one retry with exponential backoff (1s, then 2s) on throttling
or timeout errors. A second failure raises GradingError.

# Feature: verion-ai-grader, Property 4: Bedrock failure for one answer does not prevent others
"""

import base64
import json
import logging
import time
import re

import boto3
from botocore.exceptions import ClientError

from grader.exceptions import GradingError
from grader.types import CriterionScore, GradeResponse, FileAttachment

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task 8.1 — BedrockClient class
# ---------------------------------------------------------------------------

class BedrockClient:
    """
    Wraps boto3 bedrock-runtime to grade student answers using a configured model.

    Usage:
        client = BedrockClient(
            model_id="amazon.nova-pro-v1:0",
            max_tokens=2048,
            region="us-east-1",
        )
        response = client.grade_answer(
            question_body="Explain photosynthesis.",
            answer_text="Photosynthesis is...",
            rubric_criteria=[{"description": "Accuracy", "max_marks": 5}],
            question_marks=5,
        )
    """

    # Retry configuration
    _MAX_RETRIES = 3
    _BACKOFF_SECONDS = [5, 15, 30]
    _THROTTLING_ERRORS = {"ThrottlingException", "ServiceUnavailableException"}
    _TIMEOUT_ERRORS = {"RequestTimeoutException", "ReadTimeoutError"}

    def __init__(
        self,
        model_id: str,
        max_tokens: int,
        region: str,
        aws_access_key_id: str | None = None,
        aws_secret_access_key: str | None = None,
        request_delay: float = 3.0,
    ) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._request_delay = max(0.0, request_delay)
        self._last_request_at = 0.0
        client_kwargs = {"region_name": region}
        if aws_access_key_id and aws_secret_access_key:
            client_kwargs["aws_access_key_id"] = aws_access_key_id
            client_kwargs["aws_secret_access_key"] = aws_secret_access_key
        self._client = boto3.client("bedrock-runtime", **client_kwargs)

    def _is_anthropic_model(self) -> bool:
        return self._model_id.startswith("anthropic.")

    def _is_nova_model(self) -> bool:
        return self._model_id.startswith("amazon.nova")

    # ---------------------------------------------------------------------------
    # Task 8.2 — Prompt construction
    # ---------------------------------------------------------------------------

    def _build_rubric_prompt(
        self,
        question_body: str,
        answer_text: str,
        rubric_criteria: list[dict],
        has_attachment: bool = False,
    ) -> str:
        """Build a rubric-guided grading prompt."""
        rubric_lines = "\n".join(
            f"- {c['description']}: {c['max_marks']} marks"
            for c in rubric_criteria
        )
        return f"""You are an academic grader. Grade the following student answer using the provided rubric.

    Grading style requirements:
    - Be fair, constructive, and human-like. Avoid harsh wording.
    - Prioritize conceptual understanding over spelling/grammar.
    - Treat obvious minor typos (for example: "ypertext" for "hypertext") as typos, not factual inaccuracies.
    - Deduct only small marks for minor language mistakes when meaning is still clear.
    - Use major deductions only for missing core concepts, incorrect logic, or off-topic content.
    - Keep justifications specific and supportive.

Question: {question_body}

Rubric:
{rubric_lines}

Student Answer:
{answer_text if answer_text else '[See attached file]'}

{('The student response includes an attached file that must be graded together with the prompt.' if has_attachment else '')}

Return ONLY a JSON object with this exact structure:
{{
  "criteria_scores": [
    {{"criterion": "<description>", "awarded": <int>, "max": <int>, "justification": "<text>"}}
  ],
  "overall_feedback": "<text>",
  "flag": "<none|suspicious|incomplete|off_topic>",
  "flag_reason": "<text or empty>"
}}

Important:
- Output JSON only (no markdown, no code fences, no extra text).
- Ensure each awarded score is an integer between 0 and the criterion max.
"""

    def _build_holistic_prompt(
        self,
        question_body: str,
        answer_text: str,
        question_marks: int,
        has_attachment: bool = False,
    ) -> str:
        """Build a holistic grading prompt (no rubric)."""
        return f"""You are an academic grader. Grade the following student answer holistically.

    Grading style requirements:
    - Be fair, constructive, and human-like. Avoid harsh wording.
    - Prioritize conceptual understanding over spelling/grammar.
    - Treat obvious minor typos as typos, not factual inaccuracies.
    - Deduct only small marks for minor language mistakes when meaning is clear.
    - Use major deductions only for missing core concepts, incorrect logic, or off-topic content.

Question: {question_body}

Student Answer:
{answer_text if answer_text else '[See attached file]'}

{('The student response includes an attached file that must be graded together with the prompt.' if has_attachment else '')}

Score the answer holistically out of {question_marks} marks.
Return ONLY a JSON object:
{{
  "holistic_score": <int>,
  "overall_feedback": "<text>",
  "flag": "<none|suspicious|incomplete|off_topic>",
  "flag_reason": "<text or empty>"
}}

Important:
- Output JSON only (no markdown, no code fences, no extra text).
- holistic_score must be an integer between 0 and {question_marks}.
"""

    # ---------------------------------------------------------------------------
    # Task 8.3 — JSON response parsing
    # ---------------------------------------------------------------------------

    def _parse_rubric_response(self, raw: str) -> GradeResponse:
        """Parse a rubric-guided model response into a GradeResponse."""
        # Try to extract a JSON object if the model wrapped it in markdown
        candidate = self._extract_json_candidate(raw)
        if candidate is None:
            logger.warning("Bedrock response not JSON; returning safe fallback.\nPreview: %s", raw[:200])
            return GradeResponse(
                criteria_scores=[],
                holistic_score=None,
                overall_feedback="",
                flag="none",
                flag_reason=f"bedrock_parse_error: could not extract JSON (preview: {raw[:200]!r})",
            )

        try:
            data = self._parse_json_candidate(candidate)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode extracted JSON from Bedrock response: %s", exc)
            return GradeResponse(
                criteria_scores=[],
                holistic_score=None,
                overall_feedback="",
                flag="none",
                flag_reason=f"bedrock_parse_error: invalid JSON after extraction (preview: {raw[:200]!r})",
            )

        required_fields = {"criteria_scores", "overall_feedback", "flag", "flag_reason"}
        missing = required_fields - data.keys()
        if missing:
            raise GradingError(
                f"Bedrock response missing required fields: {missing}. "
                f"Raw response (first 200 chars): {raw[:200]!r}"
            )

        criteria_scores = []
        for item in data["criteria_scores"]:
            criteria_scores.append(CriterionScore(
                criterion=item.get("criterion", ""),
                awarded=int(item.get("awarded", 0)),
                max=int(item.get("max", 0)),
                justification=item.get("justification", ""),
            ))

        return GradeResponse(
            criteria_scores=criteria_scores,
            holistic_score=None,
            overall_feedback=data.get("overall_feedback", ""),
            flag=data.get("flag", "none"),
            flag_reason=data.get("flag_reason", ""),
        )

    def _parse_holistic_response(self, raw: str) -> GradeResponse:
        """Parse a holistic model response into a GradeResponse."""
        candidate = self._extract_json_candidate(raw)
        if candidate is None:
            logger.warning("Bedrock response not JSON; returning safe holistic fallback.\nPreview: %s", raw[:200])
            return GradeResponse(
                criteria_scores=[],
                holistic_score=0,
                overall_feedback="",
                flag="none",
                flag_reason=f"bedrock_parse_error: could not extract JSON (preview: {raw[:200]!r})",
            )

        try:
            data = self._parse_json_candidate(candidate)
        except json.JSONDecodeError:
            logger.warning("Failed to decode extracted JSON from Bedrock response (holistic)")
            return GradeResponse(
                criteria_scores=[],
                holistic_score=0,
                overall_feedback="",
                flag="none",
                flag_reason=f"bedrock_parse_error: invalid JSON after extraction (preview: {raw[:200]!r})",
            )

        required_fields = {"holistic_score", "overall_feedback", "flag", "flag_reason"}
        missing = required_fields - data.keys()
        if missing:
            logger.warning("Bedrock JSON missing required fields: %s", missing)
            return GradeResponse(
                criteria_scores=[],
                holistic_score=int(data.get("holistic_score", 0) or 0),
                overall_feedback=data.get("overall_feedback", ""),
                flag=data.get("flag", "none"),
                flag_reason=f"bedrock_parse_error: missing fields {missing} (preview: {raw[:200]!r})",
            )

        return GradeResponse(
            criteria_scores=[],
            holistic_score=int(data["holistic_score"]),
            overall_feedback=data.get("overall_feedback", ""),
            flag=data.get("flag", "none"),
            flag_reason=data.get("flag_reason", ""),
        )

    def _parse_json_candidate(self, candidate: str) -> dict:
        """Decode model JSON, tolerating unescaped backslashes in feedback text."""
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            # Models sometimes emit LaTeX or paths inside JSON strings without
            # escaping the backslash. Repair only invalid JSON escapes.
            repaired = re.sub(r"\\(?![\"\\/bfnrtu])", r"\\\\", candidate)
            return json.loads(repaired)

    def _extract_json_candidate(self, text: str) -> str | None:
        """Try to extract a JSON object from model output.

        Looks for a ```json fenced block first, then falls back to taking the
        substring between the first '{' and the last '}' if present.
        Returns the candidate JSON string or None if nothing found.
        """
        m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
        if m:
            return m.group(1)
        s = text.find('{')
        e = text.rfind('}')
        if s != -1 and e != -1 and e > s:
            return text[s:e+1]
        return None

    def _attachment_block(self, attachment: FileAttachment) -> dict:
        encoded = base64.b64encode(attachment.data).decode("ascii")
        if attachment.media_type == "application/pdf":
            return {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": attachment.media_type,
                    "data": encoded,
                },
            }

        if attachment.media_type.startswith("image/"):
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": attachment.media_type,
                    "data": encoded,
                },
            }

        raise GradingError(f"Unsupported attachment media type: {attachment.media_type}")

    def _build_invoke_body(self, prompt: str, attachment: FileAttachment | None = None) -> dict:
        """Build model-family specific Bedrock InvokeModel body."""
        content_blocks = [{"type": "text", "text": prompt}] if self._is_anthropic_model() else [{"text": prompt}]
        if attachment is not None:
            if not self._is_anthropic_model():
                raise GradingError(
                    "File attachments are only supported for Anthropic Claude models in this implementation."
                )
            content_blocks.append(self._attachment_block(attachment))

        if self._is_anthropic_model():
            return {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": self._max_tokens,
                "messages": [
                    {
                        "role": "user",
                        "content": content_blocks,
                    }
                ],
            }

        if self._is_nova_model():
            return {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ],
                "inferenceConfig": {
                    "max_new_tokens": self._max_tokens,
                },
            }

        # Conservative default for other message-based models.
        return {
            "messages": [
                {
                    "role": "user",
                        "content": [{"text": prompt}],
                }
            ],
        }

    def _extract_text_from_invoke_response(self, body: dict) -> str:
        """Extract generated text from known Bedrock response formats."""
        # Anthropic (Claude via messages API)
        if "content" in body and isinstance(body["content"], list):
            first = body["content"][0] if body["content"] else {}
            if isinstance(first, dict) and "text" in first:
                return first["text"]

        # Amazon Nova / Converse-style output
        try:
            return body["output"]["message"]["content"][0]["text"]
        except (KeyError, IndexError, TypeError):
            pass

        raise GradingError(
            f"Unexpected Bedrock response format. Keys: {list(body.keys())}"
        )

    # ---------------------------------------------------------------------------
    # Task 8.4 — Retry logic
    # ---------------------------------------------------------------------------

    def _invoke_with_retry(self, prompt: str, attachment: FileAttachment | None = None) -> str:
        """
        Invoke the Bedrock model with one retry on throttling/timeout.

        Returns the raw text content from the model response.
        Raises GradingError after retries are exhausted.
        """
        last_error: Exception | None = None

        for attempt in range(self._MAX_RETRIES + 1):
            try:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < self._request_delay:
                    time.sleep(self._request_delay - elapsed)
                self._last_request_at = time.monotonic()
                request_body = self._build_invoke_body(prompt, attachment)
                response = self._client.invoke_model(
                    modelId=self._model_id,
                    contentType="application/json",
                    accept="application/json",
                    body=json.dumps(request_body),
                )
                body = json.loads(response["body"].read())
                return self._extract_text_from_invoke_response(body)

            except ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                is_retryable = (
                    error_code in self._THROTTLING_ERRORS
                    or error_code in self._TIMEOUT_ERRORS
                )
                last_error = exc

                if is_retryable and attempt < self._MAX_RETRIES:
                    wait = self._BACKOFF_SECONDS[attempt]
                    logger.warning(
                        "Bedrock %s on attempt %d/%d, retrying in %ds",
                        error_code, attempt + 1, self._MAX_RETRIES + 1, wait,
                    )
                    time.sleep(wait)
                    continue

                # Non-retryable error or retries exhausted
                raise GradingError(
                    f"Bedrock ClientError ({error_code}) after {attempt + 1} attempt(s): {exc}"
                ) from exc

            except Exception as exc:
                raise GradingError(
                    f"Unexpected error invoking Bedrock: {exc}"
                ) from exc

        # Should not reach here, but satisfy type checker
        raise GradingError(
            f"Bedrock invocation failed after retries: {last_error}"
        )

    # ---------------------------------------------------------------------------
    # Task 8.1 — Public interface
    # ---------------------------------------------------------------------------

    def grade_answer(
        self,
        question_body: str,
        answer_text: str,
        rubric_criteria: list[dict],
        question_marks: int,
        attachment: FileAttachment | None = None,
    ) -> GradeResponse:
        """
        Grade a student answer using the configured Bedrock model.

        Uses a rubric-guided prompt when rubric_criteria is non-empty,
        otherwise uses a holistic prompt.

        Args:
            question_body: The question text.
            answer_text: The student's answer text.
            rubric_criteria: List of dicts with 'description' and 'max_marks'.
                             Pass an empty list for holistic grading.
            question_marks: Total marks for this question (used in holistic mode).

        Returns:
            GradeResponse with parsed scores and feedback.

        Raises:
            GradingError: If the model invocation or response parsing fails.
        """
        if rubric_criteria:
            prompt = self._build_rubric_prompt(question_body, answer_text, rubric_criteria, has_attachment=attachment is not None)
            raw = self._invoke_with_retry(prompt, attachment)
            return self._parse_rubric_response(raw)
        else:
            prompt = self._build_holistic_prompt(question_body, answer_text, question_marks, has_attachment=attachment is not None)
            raw = self._invoke_with_retry(prompt, attachment)
            return self._parse_holistic_response(raw)
