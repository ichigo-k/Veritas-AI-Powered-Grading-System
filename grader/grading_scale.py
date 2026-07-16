import json
import os
from django.core.exceptions import ImproperlyConfigured


_DEFAULT_SCALE = {"A": 70, "B": 60, "C": 50, "D": 40}


def _load_scale() -> dict[str, int]:
    raw = os.environ.get("GRADING_SCALE")
    if raw is None:
        return _DEFAULT_SCALE
    try:
        scale = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ImproperlyConfigured(f"GRADING_SCALE is not valid JSON: {exc}") from exc
    if not isinstance(scale, dict):
        raise ImproperlyConfigured("GRADING_SCALE must be a JSON object.")
    return scale


class GradingScale:
    _scale: dict[str, int] = _load_scale()

    @classmethod
    def compute_grade(cls, score: float, total_marks: int) -> str:
        if total_marks <= 0:
            return "F"
        percentage = (score / total_marks) * 100
        sorted_grades = sorted(cls._scale.items(), key=lambda item: item[1], reverse=True)
        for letter, threshold in sorted_grades:
            if percentage >= threshold:
                return letter
        return "F"


def compute_grade(score: float, total_marks: int, scale: dict[str, int] | None = None) -> str:
    if scale is None:
        return GradingScale.compute_grade(score, total_marks)
    if total_marks <= 0:
        return "F"
    percentage = (score / total_marks) * 100
    sorted_grades = sorted(scale.items(), key=lambda item: item[1], reverse=True)
    for letter, threshold in sorted_grades:
        if percentage >= threshold:
            return letter
    return "F"
