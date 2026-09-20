from __future__ import annotations

from dataclasses import dataclass
import hashlib
import statistics
import time
from typing import Any, Callable, Iterable, Literal, Protocol

from ralfloop_agent.integration.bottazzi_motor_judge import (
    BotTazziMotorJudge,
    JudgeCase,
    JudgeOutcome,
)
from ralfloop_agent.integration.motor_semantic_skeleton import (
    GrammarSessionFactory,
    SkeletonPolicy,
    compress_judge_case_facts,
)


RISK_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
TokenCounter = Callable[[JudgeCase], int]
PairOrder = Literal["raw_first", "safe_first"]


class JudgeLike(Protocol):
    def judge(self, case: JudgeCase | dict[str, Any]) -> JudgeOutcome: ...


@dataclass(frozen=True)
class EquivalenceObservation:
    case_key: str
    compressed: bool
    raw_decision: str
    safe_decision: str
    raw_gate: bool
    safe_gate: bool
    raw_risk: str
    safe_risk: str
    decision_equal: bool
    gate_equal: bool
    risk_equal: bool
    permission_relaxation: bool
    risk_relaxation: bool
    runtime_complete: bool
    raw_seconds: float
    safe_seconds: float
    raw_tokens: int | None = None
    safe_tokens: int | None = None
    guard_fallbacks: int = 0

    @property
    def unsafe_divergence(self) -> bool:
        return self.permission_relaxation or self.risk_relaxation

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "case_key": self.case_key,
            "compressed": self.compressed,
            "raw_decision": self.raw_decision,
            "safe_decision": self.safe_decision,
            "raw_gate": self.raw_gate,
            "safe_gate": self.safe_gate,
            "raw_risk": self.raw_risk,
            "safe_risk": self.safe_risk,
            "decision_equal": self.decision_equal,
            "gate_equal": self.gate_equal,
            "risk_equal": self.risk_equal,
            "permission_relaxation": self.permission_relaxation,
            "risk_relaxation": self.risk_relaxation,
            "unsafe_divergence": self.unsafe_divergence,
            "runtime_complete": self.runtime_complete,
            "raw_seconds": round(self.raw_seconds, 4),
            "safe_seconds": round(self.safe_seconds, 4),
            "guard_fallbacks": self.guard_fallbacks,
        }
        if self.raw_tokens is not None:
            payload["raw_tokens"] = self.raw_tokens
        if self.safe_tokens is not None:
            payload["safe_tokens"] = self.safe_tokens
        if self.raw_tokens and self.safe_tokens is not None:
            payload["token_ratio"] = round(self.safe_tokens / self.raw_tokens, 4)
        return payload


@dataclass(frozen=True)
class EquivalenceReport:
    observations: tuple[EquivalenceObservation, ...]
    min_pairs: int = 12

    def summary(self) -> dict[str, Any]:
        rows = self.observations
        total = len(rows)
        complete = sum(row.runtime_complete for row in rows)
        compressed = sum(row.compressed for row in rows)
        unsafe = sum(row.unsafe_divergence for row in rows)
        decisions = sum(row.decision_equal for row in rows)
        gates = sum(row.gate_equal for row in rows)
        risks = sum(row.risk_equal for row in rows)
        token_rows = [row for row in rows if row.raw_tokens is not None and row.safe_tokens is not None]
        raw_tokens = sum(int(row.raw_tokens or 0) for row in token_rows)
        safe_tokens = sum(int(row.safe_tokens or 0) for row in token_rows)
        ratios = [row.safe_tokens / row.raw_tokens for row in token_rows if row.raw_tokens]
        blockers: list[str] = []
        if total < self.min_pairs:
            blockers.append("insufficient_pairs")
        if complete != total:
            blockers.append("runtime_incomplete")
        if unsafe:
            blockers.append("unsafe_divergence")
        if decisions != total:
            blockers.append("decision_divergence")
        if gates != total:
            blockers.append("gate_divergence")
        if risks != total:
            blockers.append("risk_divergence")
        if len(token_rows) != total:
            blockers.append("token_measurement_missing")
        elif safe_tokens >= raw_tokens:
            blockers.append("no_token_saving")
        return {
            "pairs": total,
            "min_pairs": self.min_pairs,
            "runtime_complete": complete,
            "compressed_pairs": compressed,
            "unsafe_divergences": unsafe,
            "decision_agreement": round(decisions / total, 4) if total else 0.0,
            "gate_agreement": round(gates / total, 4) if total else 0.0,
            "risk_agreement": round(risks / total, 4) if total else 0.0,
            "token_pairs": len(token_rows),
            "raw_tokens": raw_tokens if token_rows else None,
            "safe_tokens": safe_tokens if token_rows else None,
            "token_ratio": round(safe_tokens / raw_tokens, 4) if raw_tokens else None,
            "median_token_ratio": round(statistics.median(ratios), 4) if ratios else None,
            "raw_seconds": round(sum(row.raw_seconds for row in rows), 3),
            "safe_seconds": round(sum(row.safe_seconds for row in rows), 3),
            "blockers": blockers,
            "promotion_allowed": not blockers,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "summary": self.summary(),
            "observations": [row.as_dict() for row in self.observations],
        }


def case_key(case: JudgeCase) -> str:
    return hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:12]


def safe_case_for_equivalence(
    case: JudgeCase,
    *,
    use_grammar: bool = True,
    grammar_session_factory: GrammarSessionFactory | None = None,
) -> tuple[JudgeCase, int, bool]:
    safe, skeleton = compress_judge_case_facts(
        case,
        policy=SkeletonPolicy(min_chars=0, min_segments=1, profile="safe"),
        use_grammar=use_grammar,
        grammar_session_factory=grammar_session_factory,
    )
    if not isinstance(safe, JudgeCase):
        safe = JudgeCase.model_validate(safe)
    return safe, int(skeleton.guard_fallbacks if skeleton else 0), skeleton is not None


def _runtime_complete(outcome: JudgeOutcome) -> bool:
    if outcome.raw_text is None:
        return False
    return outcome.verdict.reason not in {
        "judge_runtime_unavailable",
        "invalid_judge_output",
        "decision_not_in_candidate_actions",
    }


def _call(judge: JudgeLike, case: JudgeCase) -> tuple[JudgeOutcome, float]:
    started = time.monotonic()
    outcome = judge.judge(case)
    return outcome, time.monotonic() - started


def evaluate_pair(
    raw_case: JudgeCase,
    safe_case: JudgeCase,
    *,
    judge: JudgeLike,
    order: PairOrder = "raw_first",
    token_counter: TokenCounter | None = None,
    guard_fallbacks: int = 0,
    compressed: bool = True,
) -> EquivalenceObservation:
    if order == "safe_first":
        safe_outcome, safe_seconds = _call(judge, safe_case)
        raw_outcome, raw_seconds = _call(judge, raw_case)
    else:
        raw_outcome, raw_seconds = _call(judge, raw_case)
        safe_outcome, safe_seconds = _call(judge, safe_case)

    raw_tokens = token_counter(raw_case) if token_counter else None
    safe_tokens = token_counter(safe_case) if token_counter else None
    raw_v = raw_outcome.verdict
    safe_v = safe_outcome.verdict
    raw_gate = bool(raw_outcome.gate.proceed_to_next_stage)
    safe_gate = bool(safe_outcome.gate.proceed_to_next_stage)
    permission_relaxation = (
        (safe_gate and not raw_gate)
        or (safe_v.decision == "PASS" and raw_v.decision != "PASS")
    )
    risk_relaxation = RISK_RANK.get(safe_v.risk, 99) < RISK_RANK.get(raw_v.risk, 99)
    return EquivalenceObservation(
        case_key=case_key(raw_case),
        compressed=compressed,
        raw_decision=raw_v.decision,
        safe_decision=safe_v.decision,
        raw_gate=raw_gate,
        safe_gate=safe_gate,
        raw_risk=raw_v.risk,
        safe_risk=safe_v.risk,
        decision_equal=raw_v.decision == safe_v.decision,
        gate_equal=raw_gate == safe_gate,
        risk_equal=raw_v.risk == safe_v.risk,
        permission_relaxation=permission_relaxation,
        risk_relaxation=risk_relaxation,
        runtime_complete=_runtime_complete(raw_outcome) and _runtime_complete(safe_outcome),
        raw_seconds=raw_seconds,
        safe_seconds=safe_seconds,
        raw_tokens=raw_tokens,
        safe_tokens=safe_tokens,
        guard_fallbacks=guard_fallbacks,
    )


def run_equivalence_cases(
    cases: Iterable[JudgeCase],
    *,
    judge: JudgeLike | None = None,
    token_counter: TokenCounter | None = None,
    use_grammar: bool = True,
    grammar_session_factory: GrammarSessionFactory | None = None,
    min_pairs: int = 12,
) -> EquivalenceReport:
    active_judge = judge or BotTazziMotorJudge()
    observations: list[EquivalenceObservation] = []
    for index, raw_case in enumerate(cases):
        safe_case, fallbacks, compressed = safe_case_for_equivalence(
            raw_case,
            use_grammar=use_grammar,
            grammar_session_factory=grammar_session_factory,
        )
        observations.append(evaluate_pair(
            raw_case,
            safe_case,
            judge=active_judge,
            order="raw_first" if index % 2 == 0 else "safe_first",
            token_counter=token_counter,
            guard_fallbacks=fallbacks,
            compressed=compressed,
        ))
    return EquivalenceReport(tuple(observations), min_pairs=min_pairs)


__all__ = [
    "EquivalenceObservation",
    "EquivalenceReport",
    "RISK_RANK",
    "case_key",
    "evaluate_pair",
    "run_equivalence_cases",
    "safe_case_for_equivalence",
]
