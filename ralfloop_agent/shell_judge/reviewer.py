from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from .contracts import ShellDecision, ShellReviewResult


@dataclass(frozen=True)
class ReviewerClassification:
    risk: str
    authorization: str
    correctness: str
    rationale: str


class ShellReviewer:
    """Pure classifier: receives structured data and has no execution interface."""

    def __init__(self, classify: Callable[[Mapping[str, object]], Mapping[str, str]]) -> None:
        self._classify = classify

    def classify(self, result: ShellReviewResult) -> ReviewerClassification:
        if result.decision is not ShellDecision.REVIEW:
            raise ValueError("reviewer_only_accepts_review")
        raw = self._classify({
            "deterministic_reason": result.deterministic_reason,
            "risk": result.risk,
            "capabilities": {
                "commands": [list(value) for value in result.capabilities.commands],
                "read_paths": list(result.capabilities.resolved_paths_read),
                "write_paths": list(result.capabilities.resolved_paths_write),
                "delete_paths": list(result.capabilities.resolved_paths_delete),
                "unresolved": list(result.capabilities.unresolved_elements),
                "destructive_level": result.capabilities.destructive_level.value,
            },
        })
        return ReviewerClassification(
            risk=str(raw.get("risk") or "high"),
            authorization=str(raw.get("authorization") or "neutral"),
            correctness=str(raw.get("correctness") or "unknown"),
            rationale=str(raw.get("rationale") or ""),
        )


class MixtureShellReviewer:
    """Fail-closed reviewer mixture; all independent classifiers must agree."""

    def __init__(self, reviewers: tuple[ShellReviewer, ...]) -> None:
        if len(reviewers) < 2:
            raise ValueError("reviewer_mixture_requires_multiple_agents")
        self.reviewers = reviewers

    def classify(self, result: ShellReviewResult) -> ReviewerClassification:
        rows = tuple(reviewer.classify(result) for reviewer in self.reviewers)
        risk = "too_destructive" if any(row.risk == "too_destructive" for row in rows) else (
            "high" if any(row.risk == "high" for row in rows) else "low"
        )
        authorization = rows[0].authorization if len({row.authorization for row in rows}) == 1 else "neutral"
        correctness = rows[0].correctness if len({row.correctness for row in rows}) == 1 else "uncertain"
        return ReviewerClassification(
            risk=risk, authorization=authorization, correctness=correctness,
            rationale="; ".join(row.rationale for row in rows if row.rationale),
        )
