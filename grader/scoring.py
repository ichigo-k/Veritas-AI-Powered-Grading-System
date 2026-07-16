def cap_criterion_score(awarded: int, max_marks: int) -> int:
    return max(0, min(awarded, max_marks))


def compute_final_score(
    mcq_score: float,
    subjective_scores: list[float],
    total_marks: int,
) -> float:
    raw_total = mcq_score + sum(subjective_scores)
    return max(0.0, min(raw_total, float(total_marks)))
