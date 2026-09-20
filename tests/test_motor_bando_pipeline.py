from __future__ import annotations

import pytest

from ralfloop_agent.integration.bottazzi_motor_judge import (
    JudgeCase,
    JudgeGate,
    JudgeOutcome,
    JudgeVerdict,
)
from ralfloop_agent.integration.motor_bando_pipeline import (
    BandoChunk,
    BandoChunkOutcome,
    MotorBandoBudgetError,
    MotorBandoPipeline,
    MotorBandoPipelineConfig,
    aggregate_bando_outcomes,
    build_bando_chunks,
    semantic_fact_units,
)


def _case(**updates):
    data = {
        "case_id": "bando-real-name-must-not-leak",
        "goal": "Verificare ammissibilità",
        "facts": [],
        "rules": ["Non procedere se manca un requisito obbligatorio."],
        "candidate_actions": ["PASS", "REQUEST_REVIEW", "REJECT"],
    }
    data.update(updates)
    return JudgeCase(**data)

def _outcome(decision="PASS", risk="LOW", gate=True, raw_text="{}", missing=None):
    verdict = JudgeVerdict(
        decision=decision,
        confidence=0.9 if raw_text is not None else 0.0,
        risk=risk,
        reason="ok" if raw_text is not None else "judge_runtime_unavailable",
        missing_evidence=list(missing or []),
    )
    return JudgeOutcome(
        case_digest="d" * 64,
        verdict=verdict,
        gate=JudgeGate(
            proceed_to_next_stage=gate,
            status="judge_passed" if gate else "judge_uncertain",
            requires_human_review=not gate,
        ),
        raw_text=raw_text,
    )


def _chunk(index, outcome):
    case = _case(case_id=f"chunk-{index}", facts=[f"fact {index}"])
    chunk = BandoChunk(index=index, case=case, source_units=1, rendered_tokens=250, guard_fallbacks=0)
    return BandoChunkOutcome(chunk=chunk, outcome=outcome)


def test_semantic_fact_units_preserve_critical_phrases():
    units = semantic_fact_units([
        "L'ente non è ammesso se manca il RUNTS. Solo se iscritto può partecipare; entro il 30 settembre.",
    ])
    joined = " ".join(units)
    assert "non è ammesso" in joined
    assert "Solo se" in joined
    assert "30 settembre" in joined
    assert len(units) >= 2

def test_build_chunks_respects_budget_and_repeats_rules():
    case = _case(facts=[
        "Primo requisito obbligatorio.",
        "Secondo requisito obbligatorio.",
        "Terzo requisito obbligatorio.",
    ])

    def counter(chunk):
        return 170 + 55 * len(chunk.facts)

    chunks = build_bando_chunks(
        case,
        token_counter=counter,
        config=MotorBandoPipelineConfig(max_rendered_tokens=280, use_grammar=False),
    )
    assert len(chunks) == 2
    assert all(chunk.rendered_tokens <= 280 for chunk in chunks)
    assert all(chunk.case.rules == case.rules for chunk in chunks)
    assert all(chunk.case.candidate_actions == case.candidate_actions for chunk in chunks)


def test_atomic_fact_over_budget_fails_closed():
    case = _case(facts=["Requisito indivisibile molto lungo senza terminatori"])
    with pytest.raises(MotorBandoBudgetError, match="atomic_fact_exceeds_budget"):
        build_bando_chunks(
            case,
            token_counter=lambda chunk: 500,
            config=MotorBandoPipelineConfig(max_rendered_tokens=360, use_grammar=False),
        )


def test_aggregate_reject_dominates_pass():
    case = _case()
    verdict, gate = aggregate_bando_outcomes(case, [
        _chunk(0, _outcome("PASS", "LOW", True)),
        _chunk(1, _outcome("REJECT", "HIGH", False)),
    ])
    assert verdict.decision == "REJECT"
    assert verdict.risk == "HIGH"
    assert verdict.reason == "chunk_reject"
    assert not gate.proceed_to_next_stage

def test_aggregate_runtime_failure_is_uncertain():
    case = _case()
    verdict, gate = aggregate_bando_outcomes(case, [_chunk(0, _outcome(raw_text=None))])
    assert verdict.decision == "UNCERTAIN"
    assert "chunk_runtime_incomplete" in verdict.missing_evidence
    assert not gate.proceed_to_next_stage


class _QueueJudge:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.seen = []

    def judge(self, case):
        self.seen.append(case)
        return self.outcomes.pop(0)

def test_pipeline_fuses_chunk_safe_judge_and_aggregate():
    case = _case(facts=[
        "APS iscritta al RUNTS.",
        "Budget richiesto 30000 euro.",
        "Domanda firmata digitalmente.",
    ])
    judge = _QueueJudge([_outcome(), _outcome()])
    pipeline = MotorBandoPipeline(
        token_counter=lambda chunk: 170 + 55 * len(chunk.facts),
        judge=judge,
        config=MotorBandoPipelineConfig(max_rendered_tokens=280, use_grammar=False),
    )
    result = pipeline.run(case)
    payload = result.as_dict()
    assert result.verdict.decision == "PASS"
    assert result.gate.proceed_to_next_stage
    assert len(result.chunks) == 2
    assert payload["max_chunk_tokens"] <= 280
    assert "APS iscritta al RUNTS" not in repr(payload)

def test_aggregate_does_not_report_pass_when_a_chunk_gate_blocks():
    case = _case()
    verdict, gate = aggregate_bando_outcomes(case, [
        _chunk(0, _outcome("PASS", "LOW", True)),
        _chunk(1, _outcome("PASS", "LOW", False, missing=["more evidence"])),
    ])
    assert verdict.decision == "REQUEST_REVIEW"
    assert verdict.reason == "chunk_gate_blocked"
    assert not gate.proceed_to_next_stage


def test_report_is_hash_and_metrics_only():
    judge = _QueueJudge([_outcome()])
    pipeline = MotorBandoPipeline(
        token_counter=lambda chunk: 200,
        judge=judge,
        config=MotorBandoPipelineConfig(max_rendered_tokens=360, use_grammar=False),
    )
    result = pipeline.run(_case(facts=["Sensitive source sentence."]))
    rendered = repr(result.as_dict())
    assert "Sensitive source sentence" not in rendered
    assert "missing_evidence_count" in rendered


def test_chunks_use_section_local_goal_not_global_goal():
    case = _case(goal="Valuta l'intero dossier", facts=["Sezione uno.", "Sezione due."])
    chunks = build_bando_chunks(
        case,
        token_counter=lambda chunk: 200,
        config=MotorBandoPipelineConfig(max_rendered_tokens=360, use_grammar=False),
    )
    assert chunks
    assert all(chunk.case.goal != case.goal for chunk in chunks)
    assert all("section only" in chunk.case.goal for chunk in chunks)
    assert all("bando_global_goal_sha256" in chunk.case.metadata for chunk in chunks)


def test_aggregate_review_dominates_pass_without_authorizing_progress():
    case = _case()
    verdict, gate = aggregate_bando_outcomes(case, [
        _chunk(0, _outcome("PASS", "LOW", True)),
        _chunk(1, _outcome("REQUEST_REVIEW", "MEDIUM", True)),
    ])
    assert verdict.decision == "REQUEST_REVIEW"
    assert verdict.risk == "MEDIUM"
    assert verdict.reason == "chunk_review_required"
    assert not gate.proceed_to_next_stage
