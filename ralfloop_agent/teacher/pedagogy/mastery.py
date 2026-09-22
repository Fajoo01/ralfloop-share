"""Knowledge-component mastery with bounded Bayesian updates."""
from __future__ import annotations

from enum import StrEnum
from pydantic import BaseModel, ConfigDict, Field


class EvidenceType(StrEnum):
    RECOGNITION = "recognition"
    RECALL = "recall"
    APPLICATION = "application"
    EXPLANATION = "explanation"
    TRANSFER = "transfer"
    CORRECTION = "correction"


EVIDENCE_WEIGHT = {
    EvidenceType.RECOGNITION: 0.55,
    EvidenceType.RECALL: 0.75,
    EvidenceType.APPLICATION: 0.9,
    EvidenceType.EXPLANATION: 1.0,
    EvidenceType.TRANSFER: 1.0,
    EvidenceType.CORRECTION: 0.7,
}


class KnowledgeState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    component: str = Field(min_length=1, max_length=160)
    probability: float = Field(default=0.15, ge=0.01, le=0.99)
    attempts: int = Field(default=0, ge=0)
    successes: int = Field(default=0, ge=0)
    due_at: float | None = None
    last_seen: float | None = None


def _posterior(prior: float, correct: bool, *, slip: float, guess: float) -> float:
    if correct:
        numerator = prior * (1.0 - slip)
        denominator = numerator + (1.0 - prior) * guess
    else:
        numerator = prior * slip
        denominator = numerator + (1.0 - prior) * (1.0 - guess)
    return numerator / denominator if denominator else prior


def review_interval_days(probability: float, correct: bool) -> int:
    if not correct:
        return 1
    if probability >= 0.92:
        return 21
    if probability >= 0.82:
        return 10
    if probability >= 0.65:
        return 4
    return 2


def update_knowledge_state(
    state: KnowledgeState,
    *,
    correct: bool,
    evidence_type: EvidenceType,
    now: float,
    slip: float = 0.10,
    guess: float = 0.20,
    learn: float = 0.08,
) -> KnowledgeState:
    if not (0.0 <= slip < 1.0 and 0.0 <= guess < 1.0 and 0.0 <= learn < 1.0):
        raise ValueError("invalid_bkt_parameters")
    weight = EVIDENCE_WEIGHT[evidence_type]
    inferred = _posterior(state.probability, correct, slip=slip, guess=guess)
    blended = state.probability + (inferred - state.probability) * weight
    if correct:
        blended = blended + (1.0 - blended) * learn * weight
    probability = min(0.99, max(0.01, blended))
    interval = review_interval_days(probability, correct)
    return state.model_copy(update={
        "probability": probability,
        "attempts": state.attempts + 1,
        "successes": state.successes + int(correct),
        "last_seen": now,
        "due_at": now + interval * 86400,
    })
