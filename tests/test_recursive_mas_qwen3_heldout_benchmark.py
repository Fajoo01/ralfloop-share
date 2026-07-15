from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from ralfloop_agent.domains.recursive_mas_domain_dataset import deterministic_splits, generate_dataset
from ralfloop_agent.domains.recursive_mas_domain_training import _require_safe_runtime
from ralfloop_agent.domains.recursive_mas_qwen3_heldout_benchmark import (
    PRIOR_CANARY_IDS,
    RUNNERS,
    aggregate,
    audit_checkpoints,
    blind_outputs,
    differing_case_ids,
    limited_gate,
    ngram_similarity,
    precision_recall_exact,
    score_output,
    select_heldout_cases,
    validate_runner_timing,
)


def _metric(value: float = 1.0, *, valid: int = 12, schema: int = 12) -> dict:
    return {
        "semantic_complete_count": valid,
        "schema_valid_count": schema,
        "schema_validity": schema / 12,
        "rule_accuracy": value,
        "source_accuracy": value,
        "contradiction_recall": value,
        "counterargument_coverage": value,
        "uncertainty_presence": value,
        "recommendation_presence": 1.0,
        "confidence_calibration": value,
        "recommendation_usefulness": value,
        "invented_rule_ids": 0,
        "invented_source_ids": 0,
        "safety_violations": 0,
        "approval_violations": 0,
    }


def test_heldout_selection_is_deterministic_and_balanced():
    cases, _ = generate_dataset()
    splits = deterministic_splits(cases)
    first, audit = select_heldout_cases(cases, splits)
    second, _ = select_heldout_cases(cases, splits)
    assert [row["id"] for row in first] == [row["id"] for row in second]
    counts = {category: sum(row["category"] == category for row in first) for category in {row["category"] for row in first}}
    assert len(first) == 12 and set(counts.values()) == {2}
    assert sum(row["selected"] for row in audit) == 12


def test_split_and_canary_contamination_are_excluded():
    cases, _ = generate_dataset()
    splits = deterministic_splits(cases)
    selected, audit = select_heldout_cases(cases, splits)
    ids = {row["id"] for row in selected}
    assert ids <= set(splits["test"])
    assert not ids & set(splits["train"])
    assert not ids & set(splits["validation"])
    assert not ids & set(PRIOR_CANARY_IDS)
    assert all(not row["exact_duplicate"] and not row["near_duplicate"] for row in audit if row["selected"])


def test_near_duplicate_detection():
    cases, _ = generate_dataset()
    assert ngram_similarity(cases[0], cases[0]) == 1.0
    assert ngram_similarity(cases[0], cases[-1]) < 0.85
    with pytest.raises(ValueError, match="ngram_size_invalid"):
        ngram_similarity(cases[0], cases[1], 0)


def test_runner_isolation_has_four_distinct_namespaces():
    assert RUNNERS == ("A_single_domain", "B_qwen3_direct", "C_recursive_fresh", "D_recursive_trained")
    assert len(set(RUNNERS)) == 4


def test_checkpoint_hash_verification(tmp_path):
    names = ("planner_inner", "outer12", "critic_inner", "outer23", "solver_inner")
    profile = {"enabled": False, "inner_adapter_checkpoints": {}, "cross_model_adapter_checkpoints": {}}
    manifest = {}
    for index, name in enumerate(names):
        relative = Path("final") / f"{name}.pt"
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(f"weight-{index}".encode())
        target = profile["inner_adapter_checkpoints"] if "inner" in name else profile["cross_model_adapter_checkpoints"]
        target[name] = str(relative)
        manifest[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "state_shapes": {}, "dtype": ["torch.float32"], "parameters": 1, "dataset_hash": "d", "step": 10,
        }
    assert len(audit_checkpoints(profile, manifest, tmp_path)["checkpoints"]) == 5
    (tmp_path / "final/outer23.pt").write_bytes(b"bad")
    with pytest.raises(RuntimeError, match="checkpoint_hash_mismatch:outer23"):
        audit_checkpoints(profile, manifest, tmp_path)


def test_fresh_and_trained_difference_scope():
    fresh = [{"case_id": "x", "semantic_complete": False}, {"case_id": "y", "semantic_complete": True}]
    trained = [{"case_id": "x", "semantic_complete": True}, {"case_id": "y", "semantic_complete": True}]
    assert differing_case_ids(fresh, trained) == ["x"]


def test_precision_recall_scoring():
    assert precision_recall_exact(["R1", "RX"], ["R1"] ) == {"precision": 0.5, "recall": 1.0, "exact_match": False}


def test_error_attribution_prefers_upstream():
    cases, _ = generate_dataset()
    row = score_output(cases[0], "", None, parser_error="json_invalid", planner_rule_ids=[], planner_source_ids=[], runner_id="D_recursive_trained")
    assert row["error_classification"] == "upstream_provenance_error"


def test_blind_mapping_is_separate_and_complete():
    results = {name: [{"case_id": "x", "domain_opinion_v1": {"position": name}}] for name in RUNNERS}
    blind, mapping = blind_outputs(results)
    assert set(mapping) == set(RUNNERS)
    assert set(mapping.values()) == {"candidate_A", "candidate_B", "candidate_C", "candidate_D"}
    assert all("runner" not in row for row in blind)


def test_ttft_must_not_exceed_wall():
    row = {"timings": {"ttft_ms": 1.0, "generation_wall_ms": 2.0, "wall_total_ms": 3.0}}
    validate_runner_timing(row)
    row["timings"]["ttft_ms"] = 4.0
    with pytest.raises(ValueError, match="runner_timing_invalid"):
        validate_runner_timing(row)


def test_limited_gate_requires_absolute_relative_and_fresh_gain():
    values = {
        "A_single_domain": _metric(0.80),
        "B_qwen3_direct": _metric(0.80),
        "C_recursive_fresh": _metric(0.70, valid=9, schema=10),
        "D_recursive_trained": _metric(0.90),
    }
    gate = limited_gate(values)
    assert gate["passed"] is True and gate["full_36_benchmark_authorized"] is True
    values["D_recursive_trained"]["semantic_complete_count"] = 9
    assert limited_gate(values)["passed"] is False


def test_failed_gate_never_authorizes_full_benchmark():
    values = {name: _metric(1.0) for name in RUNNERS}
    gate = limited_gate(values)
    assert gate["passed"] is False
    assert gate["full_36_benchmark_authorized"] is False


def test_aggregate_uses_output_metrics_and_numeric_timing():
    row = {
        "semantic_complete": True, "schema_valid": True,
        "rule_precision": 1, "rule_recall": 1, "rule_exact_match": True,
        "source_precision": 1, "source_recall": 1, "source_exact_match": True,
        "planner_rule_precision": 1, "planner_rule_recall": 1,
        "planner_source_precision": 1, "planner_source_recall": 1,
        "contradiction_required": True, "contradiction_hit": True,
        "counterargument_coverage": 1, "uncertainty_presence": 1, "recommendation_presence": 1,
        "recommendation_condition_correct": 1, "confidence_calibration": 1, "recommendation_usefulness": 1,
        "forbidden_claims": 0, "invented_rule_ids": [], "invented_source_ids": [],
        "safety_violations": 0, "approval_violations": 0, "error_classification": None,
        "timings": {"ttft_ms": 1, "generation_wall_ms": 2, "wall_total_ms": 3},
        "gpu_peak_bytes": 4, "ram_peak_bytes": 5, "timeout": False,
    }
    assert aggregate([row])["semantic_complete_count"] == 1


def test_feature_math_and_telegram_invariants():
    root = Path(__file__).parents[1]
    assert hashlib.sha256((root / "ralfloop_agent/domains/recursive_mas_profiles.py").read_bytes()).hexdigest() == "4c89714dfabe1603c0f73d48c452d761e9d460f533c6e23f658dfdcbe72921cf"
    assert hashlib.sha256((root / "ralfloop_agent/domains/domain_approval_executor.py").read_bytes()).hexdigest() == "90031d96888449ea30baa45016f0980cf9e334f701ec037e00a46dff559db416"
    assert 'os.getenv("RALF_RECURSIVE_DOMAIN_REASONING", "0") != "0"' in inspect.getsource(_require_safe_runtime)


def test_no_automatic_judge_or_training_surface():
    source = Path(__file__).parents[1] / "ralfloop_agent/domains/recursive_mas_qwen3_heldout_benchmark.py"
    text = source.read_text()
    assert "optimizer" not in text.casefold()
    assert "codex" not in text.casefold()
    assert "human_review" not in text.casefold()
