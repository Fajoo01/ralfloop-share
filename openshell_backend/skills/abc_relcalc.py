"""Deterministic ABC relational calculator.

This module is intentionally conservative: it scores observable evidence,
keeps inference confidence capped, and always returns safe non-pressing
actions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


KIND_FACTORS: dict[str, float] = {
    "observed_fact": 1.0,
    "external_signal": 0.70,
    "inference": 0.50,
    "contradiction": 1.0,
}

MIND_READING_TAGS = {
    "mind_reading",
    "tono",
    "tone",
    "accesso_online",
    "online_access",
}

SURVEILLANCE_TAGS = {
    "accesso_online",
    "online_access",
    "tracking",
    "pedinamento",
    "sorveglianza",
    "surveillance",
    "controllo_online",
}

SAFE_ACTIONS = {
    "do_nothing_active",
    "collect_observable_evidence",
    "light_non_pressing_presence",
}


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    description: str
    weight: float = 0.0
    confidence: float = 0.5
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelCalcResult:
    score: int
    confidence: float
    bias_flags: tuple[str, ...]
    next_safe_action: str
    evidence_count: int
    summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _coerce_item(raw: EvidenceItem | dict[str, Any]) -> EvidenceItem:
    if isinstance(raw, EvidenceItem):
        return raw
    if not isinstance(raw, dict):
        raise TypeError(f"Unsupported evidence item: {type(raw)!r}")

    tags_raw = raw.get("tags", ())
    if isinstance(tags_raw, str):
        tags = (tags_raw,)
    else:
        tags = tuple(str(tag) for tag in tags_raw)

    return EvidenceItem(
        kind=str(raw.get("kind", "inference")),
        description=str(raw.get("description", "")),
        weight=float(raw.get("weight", 0.0)),
        confidence=float(raw.get("confidence", 0.5)),
        tags=tags,
    )


def calculate_relation(
    evidence: Iterable[EvidenceItem | dict[str, Any]],
    *,
    base_score: float = 50.0,
) -> RelCalcResult:
    items = [_coerce_item(item) for item in evidence]

    score = float(base_score)
    confidence_values: list[float] = []
    flags: set[str] = set()

    observed_count = 0
    inference_count = 0
    contradiction_count = 0
    high_strength_observable = False

    for item in items:
        kind = item.kind
        factor = KIND_FACTORS.get(kind, KIND_FACTORS["inference"])
        raw_confidence = clamp(item.confidence, 0.0, 1.0)

        if kind == "inference":
            inference_count += 1
            confidence = min(raw_confidence, 0.50)
            if raw_confidence > 0.50:
                flags.add("weak_evidence_high_score")
        else:
            confidence = raw_confidence

        confidence_values.append(confidence)

        tags = {tag.lower() for tag in item.tags}
        if tags & MIND_READING_TAGS:
            flags.add("mind_reading_risk")
        if tags & SURVEILLANCE_TAGS:
            flags.add("surveillance_risk")

        if kind == "observed_fact":
            observed_count += 1
            if confidence >= 0.80 and abs(item.weight) >= 15:
                high_strength_observable = True

        if kind == "external_signal" and confidence >= 0.85 and abs(item.weight) >= 18:
            high_strength_observable = True

        delta = abs(item.weight) * factor * confidence

        if kind == "contradiction":
            contradiction_count += 1
            flags.add("contradiction_present")
            score -= delta
        else:
            score += item.weight * factor * confidence

    if inference_count > observed_count:
        flags.add("too_many_inferences")

    if score > 70 and not high_strength_observable:
        score = 70
        flags.add("weak_evidence_high_score")

    score = int(round(clamp(score, 0, 100)))

    if confidence_values:
        confidence = sum(confidence_values) / len(confidence_values)
    else:
        confidence = 0.0

    if contradiction_count:
        confidence *= max(0.55, 1.0 - 0.18 * contradiction_count)

    if inference_count > observed_count:
        confidence *= 0.85

    if observed_count == 0:
        confidence = min(confidence, 0.50)

    confidence = round(clamp(confidence, 0.0, 1.0), 3)

    if "surveillance_risk" in flags or "mind_reading_risk" in flags:
        next_action = "collect_observable_evidence"
    elif "contradiction_present" in flags:
        next_action = "do_nothing_active"
    elif score >= 60 and confidence >= 0.60:
        next_action = "light_non_pressing_presence"
    else:
        next_action = "do_nothing_active"

    assert next_action in SAFE_ACTIONS

    if not items:
        summary = "No evidence provided; keep score neutral and avoid action."
    else:
        summary = (
            f"score={score}; confidence={confidence}; "
            f"evidence={len(items)}; flags={','.join(sorted(flags)) or 'none'}"
        )

    return RelCalcResult(
        score=score,
        confidence=confidence,
        bias_flags=tuple(sorted(flags)),
        next_safe_action=next_action,
        evidence_count=len(items),
        summary=summary,
    )


def calculate_relation_dict(
    evidence: Iterable[EvidenceItem | dict[str, Any]],
    *,
    base_score: float = 50.0,
) -> dict[str, Any]:
    return calculate_relation(evidence, base_score=base_score).to_dict()
