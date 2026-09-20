from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable

from ralfloop_agent.integration.bottazzi_motor_judge import (
    BotTazziMotorJudge,
    JudgeCase,
    JudgeGate,
    JudgeOutcome,
    JudgeVerdict,
    judge_case_digest,
)
from ralfloop_agent.integration.motor_semantic_equivalence import RISK_RANK
from ralfloop_agent.integration.motor_semantic_skeleton import (
    GrammarSessionFactory,
    SkeletonPolicy,
    compress_judge_case_facts,
)

DEFAULT_MAX_RENDERED_TOKENS = 360
CHUNK_GOAL = (
    "Judge this dossier section only. Ignore absent other sections. "
    "Flag only local missing/conflicting evidence; missing_evidence max 2 short items."
)
_RUNTIME_FAILURE_REASONS = frozenset({
    "judge_runtime_unavailable",
    "invalid_judge_output",
    "decision_not_in_candidate_actions",
})
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+(?=[A-ZÀ-ÖØ-Þ0-9])")


class MotorBandoBudgetError(RuntimeError):
    pass

@dataclass(frozen=True)
class MotorBandoPipelineConfig:
    max_rendered_tokens: int = DEFAULT_MAX_RENDERED_TOKENS
    use_grammar: bool = True


@dataclass(frozen=True)
class BandoChunk:
    index: int
    case: JudgeCase
    source_units: int
    rendered_tokens: int
    guard_fallbacks: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "case_key": hashlib.sha256(self.case.case_id.encode("utf-8")).hexdigest()[:12],
            "source_units": self.source_units,
            "safe_facts": len(self.case.facts),
            "rendered_tokens": self.rendered_tokens,
            "guard_fallbacks": self.guard_fallbacks,
        }


@dataclass(frozen=True)
class BandoChunkOutcome:
    chunk: BandoChunk
    outcome: JudgeOutcome

    def as_dict(self) -> dict[str, Any]:
        row = self.chunk.as_dict()
        row.update({
            "decision": self.outcome.verdict.decision,
            "risk": self.outcome.verdict.risk,
            "gate": self.outcome.gate.proceed_to_next_stage,
            "gate_status": self.outcome.gate.status,
            "runtime_complete": _runtime_complete(self.outcome),
        })
        return row


@dataclass(frozen=True)
class BandoPipelineOutcome:
    case_key: str
    case_digest: str
    chunks: tuple[BandoChunkOutcome, ...]
    verdict: JudgeVerdict
    gate: JudgeGate

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "case_key": self.case_key,
            "case_digest": self.case_digest,
            "chunk_count": len(self.chunks),
            "total_rendered_tokens": sum(row.chunk.rendered_tokens for row in self.chunks),
            "max_chunk_tokens": max((row.chunk.rendered_tokens for row in self.chunks), default=0),
            "guard_fallbacks": sum(row.chunk.guard_fallbacks for row in self.chunks),
            "runtime_complete": all(_runtime_complete(row.outcome) for row in self.chunks),
            "verdict": {
                "decision": self.verdict.decision,
                "confidence": self.verdict.confidence,
                "risk": self.verdict.risk,
                "provider": self.verdict.provider,
                "missing_evidence_count": len(self.verdict.missing_evidence),
            },
            "gate": self.gate.model_dump(),
            "chunks": [row.as_dict() for row in self.chunks],
        }


def semantic_fact_units(facts: Iterable[str]) -> tuple[str, ...]:
    units: list[str] = []
    for fact in facts:
        text = str(fact or "").replace("\r\n", "\n").strip()
        if not text:
            continue
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n|\n(?=\s*[-•])", text) if part.strip()]
        for paragraph in paragraphs:
            pieces = [part.strip() for part in _SENTENCE_SPLIT_RE.split(paragraph) if part.strip()]
            units.extend(pieces or [paragraph])
    return tuple(units)


def _chunk_case_id(case: JudgeCase, index: int) -> str:
    digest = hashlib.sha256(case.case_id.encode("utf-8")).hexdigest()[:12]
    return f"bando:{digest}:{index:03d}"


def _case_with_units(case: JudgeCase, units: Iterable[str], index: int) -> JudgeCase:
    metadata = dict(case.metadata)
    metadata["bando_global_goal_sha256"] = hashlib.sha256(
        case.goal.encode("utf-8")
    ).hexdigest()
    metadata["bando_chunk_index"] = index
    return case.model_copy(update={
        "case_id": _chunk_case_id(case, index),
        "goal": CHUNK_GOAL,
        "facts": list(units),
        "metadata": metadata,
    })

def _safe_chunk(
    case: JudgeCase,
    units: Iterable[str],
    index: int,
    *,
    token_counter: Any,
    config: MotorBandoPipelineConfig,
    grammar_session_factory: GrammarSessionFactory | None,
) -> BandoChunk:
    selected = tuple(units)
    raw = _case_with_units(case, selected, index)
    safe, skeleton = compress_judge_case_facts(
        raw,
        policy=SkeletonPolicy(min_chars=0, min_segments=1, profile="safe"),
        use_grammar=config.use_grammar,
        grammar_session_factory=grammar_session_factory,
    )
    if not isinstance(safe, JudgeCase):
        safe = JudgeCase.model_validate(safe)
    rendered_tokens = int(token_counter(safe))
    return BandoChunk(
        index=index,
        case=safe,
        source_units=len(selected),
        rendered_tokens=rendered_tokens,
        guard_fallbacks=int(skeleton.guard_fallbacks if skeleton else 0),
    )

def build_bando_chunks(
    case: JudgeCase,
    *,
    token_counter: Any,
    config: MotorBandoPipelineConfig | None = None,
    grammar_session_factory: GrammarSessionFactory | None = None,
) -> tuple[BandoChunk, ...]:
    active = config or MotorBandoPipelineConfig()
    units = semantic_fact_units(case.facts)
    if not units:
        chunk = _safe_chunk(
            case, (), 0, token_counter=token_counter, config=active,
            grammar_session_factory=grammar_session_factory,
        )
        if chunk.rendered_tokens > active.max_rendered_tokens:
            raise MotorBandoBudgetError("base_case_exceeds_budget")
        return (chunk,)

    chunks: list[BandoChunk] = []
    pending: list[str] = []
    index = 0
    for unit in units:
        candidate_units = [*pending, unit]
        candidate = _safe_chunk(
            case, candidate_units, index, token_counter=token_counter,
            config=active, grammar_session_factory=grammar_session_factory,
        )
        if candidate.rendered_tokens <= active.max_rendered_tokens:
            pending = candidate_units
            continue
        if not pending:
            raise MotorBandoBudgetError(
                f"atomic_fact_exceeds_budget:{candidate.rendered_tokens}>{active.max_rendered_tokens}"
            )
        accepted = _safe_chunk(
            case, pending, index, token_counter=token_counter,
            config=active, grammar_session_factory=grammar_session_factory,
        )
        chunks.append(accepted)
        index += 1
        pending = [unit]
        single = _safe_chunk(
            case, pending, index, token_counter=token_counter,
            config=active, grammar_session_factory=grammar_session_factory,
        )
        if single.rendered_tokens > active.max_rendered_tokens:
            raise MotorBandoBudgetError(
                f"atomic_fact_exceeds_budget:{single.rendered_tokens}>{active.max_rendered_tokens}"
            )
    if pending:
        chunks.append(_safe_chunk(
            case, pending, index, token_counter=token_counter,
            config=active, grammar_session_factory=grammar_session_factory,
        ))
    return tuple(chunks)

def _runtime_complete(outcome: JudgeOutcome) -> bool:
    return (
        outcome.raw_text is not None
        and outcome.verdict.reason not in _RUNTIME_FAILURE_REASONS
    )


def _max_risk(outcomes: Iterable[JudgeOutcome]) -> str:
    risks = [outcome.verdict.risk for outcome in outcomes]
    return max(risks, key=lambda value: RISK_RANK.get(value, 99), default="HIGH")


def _missing_evidence(outcomes: Iterable[JudgeOutcome]) -> list[str]:
    merged: list[str] = []
    for outcome in outcomes:
        for item in outcome.verdict.missing_evidence:
            if item not in merged:
                merged.append(item)
    return merged[:32]


def aggregate_bando_outcomes(
    case: JudgeCase,
    rows: Iterable[BandoChunkOutcome],
) -> tuple[JudgeVerdict, JudgeGate]:
    selected = tuple(rows)
    outcomes = tuple(row.outcome for row in selected)
    complete = bool(outcomes) and all(_runtime_complete(outcome) for outcome in outcomes)
    decisions = {outcome.verdict.decision for outcome in outcomes}
    risk = _max_risk(outcomes)
    missing = _missing_evidence(outcomes)
    all_chunk_gates = bool(outcomes) and all(
        outcome.gate.proceed_to_next_stage for outcome in outcomes
    )
    if not complete:
        decision = "UNCERTAIN"
        reason = "chunk_runtime_incomplete"
        if reason not in missing:
            missing.append(reason)
    elif "UNCERTAIN" in decisions:
        decision = "UNCERTAIN"
        reason = "chunk_uncertain"
    elif "REJECT" in decisions:
        decision = "REJECT"
        reason = "chunk_reject"
    elif "REQUEST_REVIEW" in decisions:
        decision = "REQUEST_REVIEW"
        reason = "chunk_review_required"
    elif decisions == {"PASS"}:
        if not all_chunk_gates:
            decision = "REQUEST_REVIEW"
            reason = "chunk_gate_blocked"
            if reason not in missing:
                missing.append(reason)
        else:
            decision = "PASS"
            reason = "all_chunks_pass"
    elif len(decisions) == 1:
        decision = next(iter(decisions))
        reason = "all_chunks_agree"
    else:
        decision = "UNCERTAIN"
        reason = "chunk_decision_conflict"
        if reason not in missing:
            missing.append(reason)

    confidence = min((outcome.verdict.confidence for outcome in outcomes), default=0.0)
    verdict = JudgeVerdict(
        decision=decision,
        confidence=confidence,
        risk=risk,
        reason=reason,
        missing_evidence=missing,
        provider="bottazzi_motor_bando_pipeline",
    )
    proceed = complete and decision == "PASS" and all_chunk_gates and not missing
    requires_confirmation = any(
        outcome.gate.requires_human_confirmation for outcome in outcomes
    )
    requires_review = (not proceed) or any(
        outcome.gate.requires_human_review for outcome in outcomes
    )
    gate = JudgeGate(
        proceed_to_next_stage=proceed,
        execution_authorized=False,
        status="bando_chunks_passed" if proceed else "bando_chunk_review_required",
        requires_human_confirmation=requires_confirmation,
        requires_human_review=requires_review,
    )
    return verdict, gate


class MotorBandoPipeline:
    def __init__(
        self,
        *,
        token_counter: Any,
        judge: Any | None = None,
        config: MotorBandoPipelineConfig | None = None,
        grammar_session_factory: GrammarSessionFactory | None = None,
    ) -> None:
        self.token_counter = token_counter
        self.judge = judge or BotTazziMotorJudge()
        self.config = config or MotorBandoPipelineConfig()
        self.grammar_session_factory = grammar_session_factory

    def run(self, case: JudgeCase | dict[str, Any]) -> BandoPipelineOutcome:
        dossier = case if isinstance(case, JudgeCase) else JudgeCase.model_validate(case)
        chunks = build_bando_chunks(
            dossier,
            token_counter=self.token_counter,
            config=self.config,
            grammar_session_factory=self.grammar_session_factory,
        )
        rows = tuple(
            BandoChunkOutcome(chunk=chunk, outcome=self.judge.judge(chunk.case))
            for chunk in chunks
        )
        verdict, gate = aggregate_bando_outcomes(dossier, rows)
        return BandoPipelineOutcome(
            case_key=hashlib.sha256(dossier.case_id.encode("utf-8")).hexdigest()[:12],
            case_digest=judge_case_digest(dossier),
            chunks=rows,
            verdict=verdict,
            gate=gate,
        )


__all__ = [
    "BandoChunk",
    "BandoChunkOutcome",
    "BandoPipelineOutcome",
    "DEFAULT_MAX_RENDERED_TOKENS",
    "MotorBandoBudgetError",
    "MotorBandoPipeline",
    "MotorBandoPipelineConfig",
    "aggregate_bando_outcomes",
    "build_bando_chunks",
    "semantic_fact_units",
]
