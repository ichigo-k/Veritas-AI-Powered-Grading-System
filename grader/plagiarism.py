from collections import defaultdict


class PlagiarismScanner:
    def build_collision_map(self, answers: list) -> dict[tuple[int, str], list[int]]:
        groups: dict[tuple[int, str], list[int]] = defaultdict(list)

        for answer in answers:
            if not answer.answer_hash:
                continue
            key = (answer.question_id, answer.answer_hash)
            groups[key].append(answer.attempt_id)

        return {
            key: attempt_ids
            for key, attempt_ids in groups.items()
            if len(attempt_ids) > 1
        }

    def get_flagged_attempts(self, collision_map: dict[tuple[int, str], list[int]]) -> set[int]:
        flagged: set[int] = set()
        for attempt_ids in collision_map.values():
            flagged.update(attempt_ids)
        return flagged
