"""
Shared data types for the grading service.
"""

from dataclasses import dataclass, field

@dataclass
class CriterionScore:
    """Score and justification for a single rubric criterion."""
    criterion: str
    awarded: int
    max: int
    justification: str


@dataclass
class GradeResponse:
    """
    Parsed response from a model for a single answer.

    Attributes:
        criteria_scores: Per-criterion scores (empty list for holistic grading).
        holistic_score: Score for holistic grading (None for rubric grading).
        overall_feedback: Textual feedback for the answer.
        flag: One of "none", "suspicious", "incomplete", "off_topic".
        flag_reason: Explanation of the flag (empty string if flag is "none").
    """
    criteria_scores: list[CriterionScore] = field(default_factory=list)
    holistic_score: int | None = None
    overall_feedback: str = ""
    flag: str = "none"
    flag_reason: str = ""


@dataclass(frozen=True)
class FileAttachment:
    media_type: str
    data: bytes
    filename: str | None = None
