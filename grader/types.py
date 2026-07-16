from dataclasses import dataclass, field


@dataclass
class CriterionScore:
    criterion: str
    awarded: int
    max: int
    justification: str


@dataclass
class GradeResponse:
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
