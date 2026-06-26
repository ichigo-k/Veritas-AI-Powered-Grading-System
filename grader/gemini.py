"""
Google Gemini client for AI-powered answer grading.

Calls the Generative Language REST API (no SDK dependency) with structured
prompts and parses the JSON response into GradeResponse objects. Gemini
natively accepts image and PDF attachments as inline base64 data, and supports
forced JSON output via responseMimeType.
"""

import base64
import json
import logging
import re
import time
import urllib.error
import urllib.request

from grader.exceptions import GradingError
from grader.types import CriterionScore, GradeResponse, FileAttachment

logger = logging.getLogger(__name__)

_API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# Attachment types Gemini accepts as inline data.
_SUPPORTED_MEDIA_PREFIXES = ("image/",)
_SUPPORTED_MEDIA_TYPES = {"application/pdf"}


class GeminiClient:
    """Wraps the Gemini REST API to grade student answers using a configured model."""

    _MAX_RETRIES = 1
    _BACKOFF_SECONDS = [1, 2]
    # Retry on transient/rate-limit statuses.
    _RETRYABLE_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, api_key: str, model_id: str, max_tokens: int, timeout: int = 120) -> None:
        self._api_key = api_key
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._timeout = timeout

    # -- Prompt builders (identical grading rubric to the other clients) -------

    def _build_rubric_prompt(
        self,
        question_body: str,
        answer_text: str,
        rubric_criteria: list[dict],
        has_attachment: bool = False,
    ) -> str:
        rubric_lines = "\n".join(
            f"- {c['description']}: {c['max_marks']} marks" for c in rubric_criteria
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

    # -- Response parsing ------------------------------------------------------

    def _parse_rubric_response(self, raw: str) -> GradeResponse:
        candidate = self._extract_json_candidate(raw)
        if candidate is None:
            logger.warning("Gemini response not JSON; returning safe fallback.\nPreview: %s", raw[:200])
            return GradeResponse(
                criteria_scores=[],
                holistic_score=None,
                overall_feedback="",
                flag="none",
                flag_reason=f"gemini_parse_error: could not extract JSON (preview: {raw[:200]!r})",
            )

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            logger.warning("Failed to decode extracted JSON from Gemini response")
            return GradeResponse(
                criteria_scores=[],
                holistic_score=None,
                overall_feedback="",
                flag="none",
                flag_reason=f"gemini_parse_error: invalid JSON after extraction (preview: {raw[:200]!r})",
            )

        required_fields = {"criteria_scores", "overall_feedback", "flag", "flag_reason"}
        missing = required_fields - data.keys()
        if missing:
            raise GradingError(
                f"Gemini response missing required fields: {missing}. "
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
        candidate = self._extract_json_candidate(raw)
        if candidate is None:
            logger.warning("Gemini response not JSON; returning safe holistic fallback.\nPreview: %s", raw[:200])
            return GradeResponse(
                criteria_scores=[],
                holistic_score=0,
                overall_feedback="",
                flag="none",
                flag_reason=f"gemini_parse_error: could not extract JSON (preview: {raw[:200]!r})",
            )

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            logger.warning("Failed to decode extracted JSON from Gemini response (holistic)")
            return GradeResponse(
                criteria_scores=[],
                holistic_score=0,
                overall_feedback="",
                flag="none",
                flag_reason=f"gemini_parse_error: invalid JSON after extraction (preview: {raw[:200]!r})",
            )

        required_fields = {"holistic_score", "overall_feedback", "flag", "flag_reason"}
        missing = required_fields - data.keys()
        if missing:
            logger.warning("Gemini JSON missing required fields: %s", missing)
            return GradeResponse(
                criteria_scores=[],
                holistic_score=int(data.get("holistic_score", 0) or 0),
                overall_feedback=data.get("overall_feedback", ""),
                flag=data.get("flag", "none"),
                flag_reason=f"gemini_parse_error: missing fields {missing} (preview: {raw[:200]!r})",
            )

        return GradeResponse(
            criteria_scores=[],
            holistic_score=int(data["holistic_score"]),
            overall_feedback=data.get("overall_feedback", ""),
            flag=data.get("flag", "none"),
            flag_reason=data.get("flag_reason", ""),
        )

    def _extract_json_candidate(self, text: str) -> str | None:
        m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
        if m:
            return m.group(1)
        s = text.find('{')
        e = text.rfind('}')
        if s != -1 and e != -1 and e > s:
            return text[s:e + 1]
        return None

    # -- API invocation --------------------------------------------------------

    def _build_parts(self, prompt: str, attachment: FileAttachment | None) -> list[dict]:
        parts: list[dict] = [{"text": prompt}]
        if attachment is not None:
            supported = (
                attachment.media_type.startswith(_SUPPORTED_MEDIA_PREFIXES)
                or attachment.media_type in _SUPPORTED_MEDIA_TYPES
            )
            if not supported:
                raise GradingError(
                    f"Gemini client cannot grade attachments of type "
                    f"'{attachment.media_type}'. Supported: images and PDF."
                )
            parts.append({
                "inline_data": {
                    "mime_type": attachment.media_type,
                    "data": base64.b64encode(attachment.data).decode("utf-8"),
                }
            })
        return parts

    def _invoke_with_retry(self, prompt: str, attachment: FileAttachment | None = None) -> str:
        url = f"{_API_ROOT}/{self._model_id}:generateContent?key={self._api_key}"
        payload = {
            "contents": [{"parts": self._build_parts(prompt, attachment)}],
            "generationConfig": {
                "maxOutputTokens": self._max_tokens,
                "responseMimeType": "application/json",
                "temperature": 0.2,
            },
        }
        data = json.dumps(payload).encode("utf-8")
        last_error: Exception | None = None

        for attempt in range(self._MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=self._timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                    return self._extract_text(body)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code in self._RETRYABLE_STATUS and attempt < self._MAX_RETRIES:
                    wait = self._BACKOFF_SECONDS[attempt]
                    logger.warning("Gemini HTTP %s on attempt %d, retrying in %ds", exc.code, attempt + 1, wait)
                    time.sleep(wait)
                    continue
                detail = ""
                try:
                    detail = exc.read().decode("utf-8")[:300]
                except Exception:
                    pass
                raise GradingError(f"Gemini API HTTP {exc.code} after {attempt + 1} attempt(s): {detail or exc}")
            except urllib.error.URLError as exc:
                last_error = exc
                if attempt < self._MAX_RETRIES:
                    wait = self._BACKOFF_SECONDS[attempt]
                    logger.warning("Gemini error on attempt %d, retrying in %ds: %s", attempt + 1, wait, exc)
                    time.sleep(wait)
                    continue
                raise GradingError(f"Gemini API error after {attempt + 1} attempts: {exc}")
            except Exception as exc:
                raise GradingError(f"Unexpected error invoking Gemini: {exc}")

        raise GradingError(f"Gemini invocation failed: {last_error}")

    def _extract_text(self, body: dict) -> str:
        """Pull the text out of a Gemini generateContent response."""
        try:
            candidates = body["candidates"]
            parts = candidates[0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError):
            # Surface prompt-feedback blocks (safety, etc.) when no content came back.
            feedback = body.get("promptFeedback") or body.get("candidates", [{}])
            raise GradingError(f"Unexpected Gemini response format: {str(feedback)[:300]}")

    # -- Public API ------------------------------------------------------------

    def grade_answer(
        self,
        question_body: str,
        answer_text: str,
        rubric_criteria: list[dict],
        question_marks: int,
        attachment: FileAttachment | None = None,
    ) -> GradeResponse:
        """Grade a student answer using Gemini."""
        if rubric_criteria:
            prompt = self._build_rubric_prompt(
                question_body, answer_text, rubric_criteria, has_attachment=attachment is not None
            )
            raw = self._invoke_with_retry(prompt, attachment)
            return self._parse_rubric_response(raw)
        prompt = self._build_holistic_prompt(
            question_body, answer_text, question_marks, has_attachment=attachment is not None
        )
        raw = self._invoke_with_retry(prompt, attachment)
        return self._parse_holistic_response(raw)
