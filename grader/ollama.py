"""
Ollama client for AI-powered answer grading.

Invokes a local or remote Ollama API with structured prompts and parse the
JSON response into GradeResponse objects.
"""

import json
import logging
import time
import re
import urllib.request
import urllib.error

from grader.exceptions import GradingError
from grader.types import CriterionScore, GradeResponse, FileAttachment

logger = logging.getLogger(__name__)

class OllamaClient:
    """
    Wraps Ollama API to grade student answers using a configured model.
    """

    # Retry configuration
    _MAX_RETRIES = 1
    _BACKOFF_SECONDS = [1, 2]

    def __init__(self, base_url: str, model_id: str, max_tokens: int) -> None:
        self._base_url = base_url.rstrip('/')
        self._model_id = model_id
        self._max_tokens = max_tokens

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

    def _parse_rubric_response(self, raw: str) -> GradeResponse:
        """Parse a rubric-guided model response into a GradeResponse."""
        candidate = self._extract_json_candidate(raw)
        if candidate is None:
            logger.warning("Ollama response not JSON; returning safe fallback.\nPreview: %s", raw[:200])
            return GradeResponse(
                criteria_scores=[],
                holistic_score=None,
                overall_feedback="",
                flag="none",
                flag_reason=f"ollama_parse_error: could not extract JSON (preview: {raw[:200]!r})",
            )

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            logger.warning("Failed to decode extracted JSON from Ollama response: %s", exc)
            return GradeResponse(
                criteria_scores=[],
                holistic_score=None,
                overall_feedback="",
                flag="none",
                flag_reason=f"ollama_parse_error: invalid JSON after extraction (preview: {raw[:200]!r})",
            )

        required_fields = {"criteria_scores", "overall_feedback", "flag", "flag_reason"}
        missing = required_fields - data.keys()
        if missing:
            raise GradingError(
                f"Ollama response missing required fields: {missing}. "
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
            logger.warning("Ollama response not JSON; returning safe holistic fallback.\nPreview: %s", raw[:200])
            return GradeResponse(
                criteria_scores=[],
                holistic_score=0,
                overall_feedback="",
                flag="none",
                flag_reason=f"ollama_parse_error: could not extract JSON (preview: {raw[:200]!r})",
            )

        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            logger.warning("Failed to decode extracted JSON from Ollama response (holistic)")
            return GradeResponse(
                criteria_scores=[],
                holistic_score=0,
                overall_feedback="",
                flag="none",
                flag_reason=f"ollama_parse_error: invalid JSON after extraction (preview: {raw[:200]!r})",
            )

        required_fields = {"holistic_score", "overall_feedback", "flag", "flag_reason"}
        missing = required_fields - data.keys()
        if missing:
            logger.warning("Ollama JSON missing required fields: %s", missing)
            return GradeResponse(
                criteria_scores=[],
                holistic_score=int(data.get("holistic_score", 0) or 0),
                overall_feedback=data.get("overall_feedback", ""),
                flag=data.get("flag", "none"),
                flag_reason=f"ollama_parse_error: missing fields {missing} (preview: {raw[:200]!r})",
            )

        return GradeResponse(
            criteria_scores=[],
            holistic_score=int(data["holistic_score"]),
            overall_feedback=data.get("overall_feedback", ""),
            flag=data.get("flag", "none"),
            flag_reason=data.get("flag_reason", ""),
        )

    def _extract_json_candidate(self, text: str) -> str | None:
        """Try to extract a JSON object from model output."""
        m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', text, re.DOTALL)
        if m:
            return m.group(1)
        s = text.find('{')
        e = text.rfind('}')
        if s != -1 and e != -1 and e > s:
            return text[s:e+1]
        return None

    def _invoke_with_retry(self, prompt: str, attachment: FileAttachment | None = None) -> str:
        """Invoke Ollama API with retry logic."""
        last_error: Exception | None = None

        url = f"{self._base_url}/api/generate"

        # Ollama doesn't support PDF directly in 'generate' the same way as Claude via Bedrock
        # For now, we'll just send the prompt.
        # If it's an image, Ollama supports 'images' field (base64 strings)

        payload = {
            "model": self._model_id,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": self._max_tokens,
            }
        }

        if attachment and attachment.media_type.startswith("image/"):
            import base64
            payload["images"] = [base64.b64encode(attachment.data).decode("utf-8")]

        for attempt in range(self._MAX_RETRIES + 1):
            try:
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=60) as response:
                    res_data = json.loads(response.read().decode("utf-8"))
                    return res_data.get("response", "")

            except (urllib.error.URLError, urllib.error.HTTPError) as exc:
                last_error = exc
                if attempt < self._MAX_RETRIES:
                    wait = self._BACKOFF_SECONDS[attempt]
                    logger.warning("Ollama error on attempt %d, retrying in %ds: %s", attempt + 1, wait, exc)
                    time.sleep(wait)
                    continue
                raise GradingError(f"Ollama API error after {attempt + 1} attempts: {exc}")
            except Exception as exc:
                raise GradingError(f"Unexpected error invoking Ollama: {exc}")

        raise GradingError(f"Ollama invocation failed: {last_error}")

    def grade_answer(
        self,
        question_body: str,
        answer_text: str,
        rubric_criteria: list[dict],
        question_marks: int,
        attachment: FileAttachment | None = None,
    ) -> GradeResponse:
        """Grade a student answer using Ollama."""
        if rubric_criteria:
            prompt = self._build_rubric_prompt(question_body, answer_text, rubric_criteria, has_attachment=attachment is not None)
            raw = self._invoke_with_retry(prompt, attachment)
            return self._parse_rubric_response(raw)
        else:
            prompt = self._build_holistic_prompt(question_body, answer_text, question_marks, has_attachment=attachment is not None)
            raw = self._invoke_with_retry(prompt, attachment)
            return self._parse_holistic_response(raw)
