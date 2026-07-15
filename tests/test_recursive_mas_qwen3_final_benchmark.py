from __future__ import annotations

import hashlib
import inspect
from pathlib import Path

import pytest

from ralfloop_agent.domains.recursive_mas_domain_dataset import deterministic_splits, generate_dataset
from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
from ralfloop_agent.domains.recursive_mas_qwen3_final_benchmark import (
    OBSERVED_12,
    contamination_audit,
    enforce_untouched_first,
    final_technical_gate,
    freeze_manifest,
    partition_final_test,
    planner_provenance_metrics,
    primary_error_attribution,
    timing_summary,
    verify_frozen_manifest,
)
from ralfloop_agent.domains.recursive_mas_qwen3_heldout_benchmark import PRIOR_CANARY_IDS


def _aggregate(value: float = 1.0, *, valid: int = 24, schema: int = 24) -> dict:
    return {
        "semantic_complete_count": valid,
        "schema_valid_count": schema,
        "schema_validity": schema / 24,
        "rule_recall": value,
        "source_recall": value,
        "contradiction_recall": value,
        "counterargument_coverage": value,
        "uncertainty_presence": value,
        "recommendation_presence": 1.0,
        "confidence_calibration": value,
        "recommendation_condition_correctness": value,
        "invented_rule_ids": 0,
        "invented_source_ids": 0,
        "safety_violations": 0,
        "approval_violations": 0,
    }


def test_observed_untouched_combined_partitions():
    cases, _ = generate_dataset()
    partitions = partition_final_test(cases, deterministic_splits(cases))
    assert len(partitions["observed_12"]) == 12
    assert len(partitions["untouched_24"]) == 24
    assert len(partitions["combined_36"]) == 36
    assert {case["id"] for case in partitions["observed_12"]} == set(OBSERVED_12)
    assert not {case["id"] for case in partitions["observed_12"]} & {case["id"] for case in partitions["untouched_24"]}


def test_prior_canary_contaminates_nominal_untouched_partition():
    cases, _ = generate_dataset()
    splits = deterministic_splits(cases)
    partitions = partition_final_test(cases, splits)
    by_id = {case["id"]: case for case in cases}
    sources = {case_id: ["prior_canary_registry"] for case_id in PRIOR_CANARY_IDS}
    references = [by_id[case_id] for case_id in PRIOR_CANARY_IDS]
    audit = contamination_audit(partitions["untouched_24"], references, observed_sources=sources)
    assert audit["classification"] == "final_holdout_contamination"
    assert set(audit["contaminated_case_ids"]) == set(PRIOR_CANARY_IDS)


def test_manifest_freeze_and_hash_verification():
    keys = (
        "git_commit", "dataset_hash", "split_hash", "checkpoint_hashes", "model_revisions",
        "tokenizer_hashes", "prompt_hash", "parser_hash", "serializer_hash",
        "evidence_packet_schema_hash", "runner_hash",
    )
    manifest = freeze_manifest({key: key for key in keys})
    verify_frozen_manifest(manifest)
    manifest["prompt_hash"] = "changed"
    with pytest.raises(RuntimeError, match="frozen_manifest_changed"):
        verify_frozen_manifest(manifest)


def test_untouched_first_and_contamination_enforced():
    with pytest.raises(RuntimeError, match="untouched_first_violation"):
        enforce_untouched_first({"phase": "manifest_frozen", "contaminated": False}, "combined_36")
    with pytest.raises(RuntimeError, match="final_holdout_contamination"):
        enforce_untouched_first({"phase": "manifest_frozen", "contaminated": True}, "untouched_24")
    enforce_untouched_first({"phase": "untouched_24_complete", "contaminated": False}, "combined_36")


def test_metric_names_keep_precision_recall_distinct():
    rows = [{
        "selected_rule_ids": ["R1", "RX"], "gold_rule_ids": ["R1"],
        "selected_source_ids": ["S1"], "gold_source_ids": ["S1", "S2"],
    }]
    value = planner_provenance_metrics(rows)
    assert value["rule_recall"] == 1.0 and value["rule_precision"] == 0.5
    assert value["source_recall"] == 0.5 and value["source_precision"] == 1.0
    assert "accuracy" not in value


def test_primary_error_is_single_and_secondary_preserved():
    result = primary_error_attribution({"planner_overselection": True, "solver_format_error": True})
    assert result == {"primary": "planner_overselection", "secondary": ["solver_format_error"]}


def test_runner_timing_requires_ttft_not_after_wall():
    rows = [{"timings": {"ttft_ms": 1.0, "wall_total_ms": 3.0}}]
    assert timing_summary(rows)["wall_median_ms"] == 3.0
    rows[0]["timings"]["ttft_ms"] = 4.0
    with pytest.raises(ValueError, match="runner_timing_invalid"):
        timing_summary(rows)


def test_final_gate_requires_absolute_relative_and_fresh_gain():
    values = {
        "A_single_domain": _aggregate(0.75),
        "B_qwen3_direct": _aggregate(0.80),
        "C_recursive_fresh": _aggregate(0.70, valid=10, schema=10),
        "D_recursive_trained": _aggregate(0.90),
    }
    result = final_technical_gate(values)
    assert result["passed"] and result["planner_refinement_authorized"]
    assert not result["production_authorized"]
    values["D_recursive_trained"]["semantic_complete_count"] = 20
    assert not final_technical_gate(values)["passed"]


def test_no_automatic_planner_refinement_or_training_surface():
    source = Path(__file__).parents[1] / "ralfloop_agent/domains/recursive_mas_qwen3_final_benchmark.py"
    text = source.read_text().casefold()
    assert "optimizer" not in text
    assert "backward(" not in text
    assert "planner_refinement_executed" not in text


def test_feature_math_and_telegram_invariants():
    root = Path(__file__).parents[1]
    assert hashlib.sha256((root / "ralfloop_agent/domains/recursive_mas_profiles.py").read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256((root / "ralfloop_agent/domains/domain_approval_executor.py").read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)
