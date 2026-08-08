from __future__ import annotations

import pytest

from ralfloop_agent.domains.recursive_mas_domain_instruct_ablation import (
    QUALITY_METRICS,
    ablation_preflight,
    adoption_gate,
    aggregate_case_metrics,
    first_systematic_regression,
    progressive_ablation_plan,
    test_split_allowed as split_allowed,
)


def _metrics(value=1.0):
    return {name: value for name in QUALITY_METRICS}


def test_progressive_ablation_plan_matches_required_order_and_modes():
    plan = progressive_ablation_plan()
    assert [item["id"] for item in plan] == list("ABCDEFG")
    assert plan[0]["name"] == "solver_instruct_direct_text"
    assert plan[2]["name"] == "gold_critic_hidden_cross_to_solver"
    assert plan[-1]["name"] == "complete_latent_pipeline"
    assert all(item["external_actions"] is False and item["approvals"] is False for item in plan)


def test_ablation_aggregate_contains_contract_and_semantic_metrics():
    metrics = aggregate_case_metrics([_metrics(1.0), _metrics(0.5)])
    assert metrics == {name: 0.75 for name in QUALITY_METRICS}


def test_progressive_ablation_stops_at_first_protected_regression_and_names_arc():
    results = {name: _metrics(1.0) for name in "ABCDE"}
    results["E"] = {**_metrics(1.0), "source_accuracy": 0.75}
    result = first_systematic_regression(results)
    assert result["stopped"] is True
    assert result["stage"] == "E"
    assert result["responsible_arc"] == "planner_inner_outer_12"
    assert result["regressed_metrics"] == ["source_accuracy"]


def test_progressive_ablation_stops_when_next_stage_was_not_run():
    result = first_systematic_regression({"A": _metrics()})
    assert result["reason"] == "next_ablation_not_evaluated"
    assert result["next"] == "B"


def test_ablation_is_blocked_before_direct_and_micro_output_gates():
    with pytest.raises(RuntimeError, match="instruct_base_insufficient"):
        ablation_preflight(direct_gate={"passed": False}, pipeline_micro_gate={"passed": False})
    with pytest.raises(RuntimeError, match="instruct_pipeline_micro_overfit_required"):
        ablation_preflight(direct_gate={"passed": True}, pipeline_micro_gate={"passed": False})


def test_test_split_is_blocked_until_pipeline_micro_overfit_passes():
    assert split_allowed({"passed": False}) is False
    assert split_allowed({"passed": True}) is True


def test_adoption_requires_ten_percent_relative_improvement_without_regressions():
    baseline = {
        "contradiction_recall": 0.5,
        "counterargument_coverage": 0.5,
        "uncertainty_calibration": 0.5,
        "recommendation_usefulness": 0.5,
        "schema_validity": 1.0,
        "rule_accuracy": 1.0,
        "source_accuracy": 1.0,
        "hallucination_rate": 0.0,
        "safety": 1.0,
        "approval_invariants": 1.0,
    }
    candidate = {**baseline, "contradiction_recall": 0.55}
    result = adoption_gate(baseline, candidate)
    assert result["passed"] is True
    assert result["improved_metrics"] == ["contradiction_recall"]
    assert result["production_enable_allowed"] is False


def test_adoption_rejects_hallucination_or_contract_regression():
    baseline = {
        "contradiction_recall": 0.5,
        "counterargument_coverage": 0.5,
        "uncertainty_calibration": 0.5,
        "recommendation_usefulness": 0.5,
        "schema_validity": 1.0,
        "rule_accuracy": 1.0,
        "source_accuracy": 1.0,
        "hallucination_rate": 0.0,
        "safety": 1.0,
        "approval_invariants": 1.0,
    }
    candidate = {**baseline, "recommendation_usefulness": 0.6, "schema_validity": 0.9, "hallucination_rate": 0.1}
    result = adoption_gate(baseline, candidate)
    assert result["passed"] is False
    assert set(result["regressions"]) == {"schema_validity", "hallucination_rate"}
