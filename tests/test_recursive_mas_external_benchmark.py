from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
from ralfloop_agent.domains.recursive_mas_external_benchmark import (
    aggregate_external,
    enrich_score,
    external_gate,
    load_external_cases,
    select_infrastructure_canary,
    trained_exceeds_direct,
    verify_dataset_freeze,
)
from ralfloop_agent.domains.recursive_mas_external_holdouts import generate_external_holdouts, jsonl_bytes
from ralfloop_agent.domains.recursive_mas_qwen3_heldout_benchmark import RUNNERS


def _row(case_id: str) -> dict:
    return {
        "case_id": case_id, "runner_id": "D_recursive_trained", "semantic_complete": True, "schema_valid": True,
        "rule_precision": 1.0, "rule_recall": 1.0, "rule_exact_match": True,
        "source_precision": 1.0, "source_recall": 1.0, "source_exact_match": True,
        "planner_rule_precision": 1.0, "planner_rule_recall": 1.0, "planner_source_precision": 1.0, "planner_source_recall": 1.0,
        "contradiction_required": True, "contradiction_hit": True, "counterargument_coverage": 1.0,
        "uncertainty_presence": 1.0, "recommendation_presence": 1.0, "recommendation_condition_correct": 1.0,
        "confidence_calibration": 1.0, "recommendation_usefulness": 1.0, "forbidden_claims": 0,
        "invented_rule_ids": [], "invented_source_ids": [], "safety_violations": 0, "approval_violations": 0,
        "error_classification": None, "human_decision_correct": 1.0,
        "timings": {"load_ms": 1.0, "ttft_ms": 1.0, "generation_wall_ms": 2.0, "wall_total_ms": 3.0},
        "gpu_peak_bytes": 4, "ram_peak_bytes": 5, "timeout": False, "domain_opinion_v1": {"human_decision_required": True},
        "primary_error": None,
    }


def _aggregate(value: float, valid: int = 36, schema: int = 36) -> dict:
    return {
        "semantic_complete_count": valid, "schema_valid_count": schema, "schema_validity": schema / 36,
        "rule_recall": value, "source_recall": value, "contradiction_recall": value,
        "counterargument_coverage": value, "uncertainty_presence": value, "recommendation_presence": 1.0,
        "confidence_calibration": value, "recommendation_condition_correctness": value,
        "invented_rule_ids": 0, "invented_source_ids": 0, "safety_violations": 0, "approval_violations": 0,
    }


def test_explicit_final_path_and_no_reserve_execution(tmp_path):
    final, reserve = generate_external_holdouts()
    final_path, reserve_path = tmp_path / "final.jsonl", tmp_path / "reserve.jsonl"
    final_path.write_bytes(jsonl_bytes(final))
    reserve_path.write_bytes(jsonl_bytes(reserve))
    assert len(load_external_cases(final_path)) == 36
    with pytest.raises(RuntimeError, match="reserve_b_execution_forbidden"):
        load_external_cases(reserve_path)


def test_dataset_freeze_detects_change(tmp_path):
    final, _ = generate_external_holdouts()
    path = tmp_path / "final.jsonl"
    path.write_bytes(jsonl_bytes(final))
    manifest = {"frozen": True, "FINAL_A": {"dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}}
    verify_dataset_freeze(path, manifest, "FINAL_A")
    path.write_text("changed")
    with pytest.raises(RuntimeError, match="external_dataset_freeze_mismatch"):
        verify_dataset_freeze(path, manifest, "FINAL_A")


def test_runner_isolation_and_fresh_trained_names():
    assert RUNNERS == ("A_single_domain", "B_qwen3_direct", "C_recursive_fresh", "D_recursive_trained")
    assert len(set(RUNNERS)) == 4


def test_canary_one_case_per_category():
    final, _ = generate_external_holdouts()
    selected = select_infrastructure_canary(final)
    assert len(selected) == 6 and len({case["category"] for case in selected}) == 6


def test_metric_naming_and_macro_micro_aggregation():
    final, _ = generate_external_holdouts()
    rows = [_row(case["id"]) for case in final]
    result = aggregate_external(rows, {case["id"]: case for case in final})
    assert "rule_recall" in result and "rule_precision" in result and "rule_accuracy" not in result
    assert len(result["macro_by_category"]) == 6 and len(result["macro_by_domain"]) == 6
    assert result["micro_average"]["human_decision_correct"] == 1.0


def test_error_attribution_prefers_planner_missing():
    final, _ = generate_external_holdouts()
    case = final[0]
    row = _row(case["id"])
    upstream = {"cases": {case["id"]: {"planner": {"structured_output": {"rules_selected": [], "sources_selected": []}}, "evidence_packet": {}}}}
    enriched = enrich_score(row, case, upstream)
    assert enriched["primary_error"] == "planner_missing_evidence"


def test_outer23_ablation_isolated_surface():
    tool = Path(__file__).parents[1] / "tools/run_recursive_mas_external_benchmark.py"
    source = tool.read_text()
    assert '"D4_without_outer23"' in source
    assert "D1_without_planner_inner" not in source
    assert "D2_without_outer12" not in source
    assert "D3_without_critic_inner" not in source
    assert "D5_without_solver_inner" not in source


def test_external_final_gate_and_no_production():
    values = {
        "A_single_domain": _aggregate(0.70), "B_qwen3_direct": _aggregate(0.80),
        "C_recursive_fresh": _aggregate(0.60, 10, 10), "D_recursive_trained": _aggregate(0.90),
    }
    gate = external_gate(values)
    assert gate["passed"] and gate["planner_refinement_authorized"] and not gate["production_authorized"]
    assert trained_exceeds_direct(values)
    values["D_recursive_trained"]["semantic_complete_count"] = 31
    assert not external_gate(values)["passed"]


def test_no_automatic_planner_refinement_or_training():
    module = Path(__file__).parents[1] / "ralfloop_agent/domains/recursive_mas_external_benchmark.py"
    source = module.read_text().casefold()
    assert "optimizer" not in source and "backward(" not in source


def test_feature_math_and_telegram_invariants():
    root = Path(__file__).parents[1]
    assert hashlib.sha256((root / "ralfloop_agent/domains/recursive_mas_profiles.py").read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256((root / "ralfloop_agent/domains/domain_approval_executor.py").read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
