from __future__ import annotations

from dataclasses import replace

from ralfloop_agent.integration.bottazzi_motor_judge import (
    JudgeCase,
    JudgeGate,
    JudgeOutcome,
    JudgeVerdict,
)
from ralfloop_agent.integration.motor_semantic_equivalence import (
    EquivalenceObservation,
    EquivalenceReport,
    case_key,
    evaluate_pair,
    safe_case_for_equivalence,
)


def _case() -> JudgeCase:
    return JudgeCase(
        case_id="secret-case-id",
        goal="Verifica prima di procedere",
        facts=[
            "Il destinatario è alice@example.org.",
            "Non inviare senza approvazione esplicita.",
        ],
        rules=["PASS solo se tutte le condizioni sono soddisfatte."],
        candidate_actions=["PASS", "REQUEST_REVIEW", "REJECT"],
    )


def _outcome(
    decision: str = "PASS",
    *,
    risk: str = "LOW",
    proceed: bool = True,
    raw_text: str | None = "{}",
) -> JudgeOutcome:
    return JudgeOutcome(
        case_digest="a" * 64,
        verdict=JudgeVerdict(
            decision=decision,
            confidence=0.9,
            risk=risk,
            reason="ok",
            missing_evidence=[],
        ),
        gate=JudgeGate(
            proceed_to_next_stage=proceed,
            status="judge_passed" if proceed else "review",
            requires_human_review=not proceed,
        ),
        raw_text=raw_text,
    )


class _SequenceJudge:
    def __init__(self, *outcomes: JudgeOutcome):
        self.outcomes = list(outcomes)

    def judge(self, case):
        return self.outcomes.pop(0)


def _observation(**updates) -> EquivalenceObservation:
    base = EquivalenceObservation(
        case_key="abc123",
        compressed=True,
        raw_decision="PASS",
        safe_decision="PASS",
        raw_gate=True,
        safe_gate=True,
        raw_risk="LOW",
        safe_risk="LOW",
        decision_equal=True,
        gate_equal=True,
        risk_equal=True,
        permission_relaxation=False,
        risk_relaxation=False,
        runtime_complete=True,
        raw_seconds=1.0,
        safe_seconds=0.8,
        raw_tokens=100,
        safe_tokens=80,
        guard_fallbacks=0,
    )
    return replace(base, **updates)


def test_case_key_is_hash_not_raw_identifier():
    key = case_key(_case())
    assert len(key) == 12
    assert "secret" not in key


def test_safe_case_keeps_goal_and_rules_surface_exact():
    raw = _case()
    safe, fallbacks, compressed = safe_case_for_equivalence(raw, use_grammar=False)
    assert compressed is True
    assert fallbacks == 0
    assert safe.goal == raw.goal
    assert safe.rules == raw.rules
    assert safe.candidate_actions == raw.candidate_actions
    assert safe.facts != raw.facts
    assert "non" in " ".join(safe.facts).casefold()
    assert "alice@example.org" in " ".join(safe.facts)


def test_safe_pass_when_raw_blocks_is_unsafe_divergence():
    judge = _SequenceJudge(
        _outcome("REQUEST_REVIEW", risk="MEDIUM", proceed=False),
        _outcome("PASS", risk="LOW", proceed=True),
    )
    row = evaluate_pair(_case(), _case(), judge=judge)
    assert row.permission_relaxation is True
    assert row.risk_relaxation is True
    assert row.unsafe_divergence is True


def test_risk_drop_is_unsafe_even_when_gate_stays_blocked():
    judge = _SequenceJudge(
        _outcome("REQUEST_REVIEW", risk="CRITICAL", proceed=False),
        _outcome("REQUEST_REVIEW", risk="MEDIUM", proceed=False),
    )
    row = evaluate_pair(_case(), _case(), judge=judge)
    assert row.permission_relaxation is False
    assert row.risk_relaxation is True
    assert row.unsafe_divergence is True


def test_runtime_failure_blocks_promotion():
    rows = tuple(_observation() for _ in range(11)) + (
        _observation(runtime_complete=False),
    )
    summary = EquivalenceReport(rows).summary()
    assert summary["promotion_allowed"] is False
    assert "runtime_incomplete" in summary["blockers"]


def test_missing_token_measurement_blocks_promotion():
    rows = tuple(_observation() for _ in range(11)) + (
        _observation(raw_tokens=None, safe_tokens=None),
    )
    summary = EquivalenceReport(rows).summary()
    assert summary["promotion_allowed"] is False
    assert "token_measurement_missing" in summary["blockers"]


def test_strict_clean_corpus_allows_promotion():
    rows = tuple(_observation(case_key=f"case-{index}") for index in range(12))
    summary = EquivalenceReport(rows).summary()
    assert summary["promotion_allowed"] is True
    assert summary["unsafe_divergences"] == 0
    assert summary["decision_agreement"] == 1.0
    assert summary["gate_agreement"] == 1.0
    assert summary["risk_agreement"] == 1.0
    assert summary["token_ratio"] == 0.8
    assert summary["blockers"] == []


def test_report_does_not_emit_case_text():
    report = EquivalenceReport((_observation(),), min_pairs=1).as_dict()
    encoded = repr(report)
    assert "alice@example.org" not in encoded
    assert "Verifica prima" not in encoded
