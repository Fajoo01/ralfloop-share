from __future__ import annotations

from collections import Counter
import statistics
from typing import Any, Iterable, Mapping, Sequence

from .recursive_mas_qwen3_heldout_benchmark import (
    RUNNERS,
    ngram_similarity,
    stable_sha256,
)


OBSERVED_12 = (
    "domain_adapter_conf_07_03",
    "domain_adapter_conf_08_02",
    "domain_adapter_doma_06_01",
    "domain_adapter_doma_06_05",
    "domain_adapter_evid_04_05",
    "domain_adapter_evid_05_01",
    "domain_adapter_inco_04_01",
    "domain_adapter_inco_08_05",
    "domain_adapter_reco_04_05",
    "domain_adapter_reco_08_05",
    "domain_adapter_stra_06_04",
    "domain_adapter_stra_07_03",
)

PRIMARY_ERRORS = (
    "planner_overselection",
    "planner_missing_evidence",
    "critic_overselection",
    "critic_missing_evidence",
    "upstream_provenance_error",
    "adapter_transfer_error",
    "solver_semantic_error",
    "solver_format_error",
    "serialization_error",
    "infrastructure_error",
)

FINAL_QUALITATIVE_METRICS = (
    "contradiction_recall",
    "counterargument_coverage",
    "confidence_calibration",
    "recommendation_condition_correctness",
)


def partition_final_test(
    cases: Sequence[Mapping[str, Any]], splits: Mapping[str, Sequence[str]]
) -> dict[str, list[Mapping[str, Any]]]:
    by_id = {str(case["id"]): case for case in cases}
    test_ids = [str(case_id) for case_id in splits["test"]]
    observed = set(OBSERVED_12)
    if len(test_ids) != 36 or len(observed) != 12 or not observed <= set(test_ids):
        raise RuntimeError("final_test_partition_invalid")
    return {
        "observed_12": [by_id[case_id] for case_id in test_ids if case_id in observed],
        "untouched_24": [by_id[case_id] for case_id in test_ids if case_id not in observed],
        "combined_36": [by_id[case_id] for case_id in test_ids],
    }


def contamination_audit(
    primary: Sequence[Mapping[str, Any]],
    references: Sequence[Mapping[str, Any]],
    *,
    observed_sources: Mapping[str, Sequence[str]],
    near_duplicate_threshold: float = 0.85,
) -> dict[str, Any]:
    reference_inputs: dict[str, set[str]] = {}
    reference_gold: dict[str, set[str]] = {}
    for case in references:
        reference_inputs.setdefault(stable_sha256(_input(case)), set()).add(str(case["id"]))
        reference_gold.setdefault(stable_sha256(case["gold"]), set()).add(str(case["id"]))
    observed_ids = set(observed_sources)
    rows = []
    for case in primary:
        case_id = str(case["id"])
        input_hash = stable_sha256(_input(case))
        gold_hash = stable_sha256(case["gold"])
        comparable = [other for other in references if str(other["id"]) != case_id]
        similarity, nearest = max(
            ((ngram_similarity(case, other, 5), str(other["id"])) for other in comparable),
            default=(0.0, ""),
        )
        row = {
            "case_id": case_id,
            "case_id_overlap": case_id in observed_ids,
            "observed_sources": sorted(set(observed_sources.get(case_id, ()))),
            "input_hash": input_hash,
            "input_hash_overlap": sorted(reference_inputs.get(input_hash, set())),
            "gold_hash": gold_hash,
            "gold_hash_overlap": sorted(reference_gold.get(gold_hash, set())),
            "max_5gram_similarity": similarity,
            "nearest_reference_case_id": nearest,
            "exact_duplicate": bool(reference_inputs.get(input_hash) or reference_gold.get(gold_hash)),
            "near_duplicate": similarity >= near_duplicate_threshold,
        }
        row["contaminated"] = bool(
            row["case_id_overlap"]
            or row["input_hash_overlap"]
            or row["gold_hash_overlap"]
            or row["near_duplicate"]
        )
        rows.append(row)
    contaminated_ids = [row["case_id"] for row in rows if row["contaminated"]]
    return {
        "classification": "final_holdout_contamination" if contaminated_ids else None,
        "contaminated": bool(contaminated_ids),
        "contaminated_count": len(contaminated_ids),
        "contaminated_case_ids": contaminated_ids,
        "max_5gram_similarity": max((row["max_5gram_similarity"] for row in rows), default=0.0),
        "rows": rows,
    }


def _input(case: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: case[key]
        for key in (
            "domain_id",
            "domain_version",
            "question",
            "facts",
            "rules",
            "sources",
            "constraints",
            "known_contradictions",
        )
    }


def freeze_manifest(fields: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "git_commit",
        "dataset_hash",
        "split_hash",
        "checkpoint_hashes",
        "model_revisions",
        "tokenizer_hashes",
        "prompt_hash",
        "parser_hash",
        "serializer_hash",
        "evidence_packet_schema_hash",
        "runner_hash",
    }
    missing = sorted(required - set(fields))
    if missing:
        raise ValueError(f"frozen_manifest_missing:{','.join(missing)}")
    manifest = dict(fields)
    manifest.update(
        {
            "protocol": "recursive_domain_qwen3_final_holdout_v1",
            "frozen": True,
            "untouched_first_required": True,
            "training_executed": False,
            "models_downloaded": False,
        }
    )
    manifest["freeze_hash"] = stable_sha256(manifest)
    return manifest


def verify_frozen_manifest(manifest: Mapping[str, Any]) -> None:
    value = dict(manifest)
    expected = str(value.pop("freeze_hash", ""))
    if not value.get("frozen") or stable_sha256(value) != expected:
        raise RuntimeError("frozen_manifest_changed")


def enforce_untouched_first(state: Mapping[str, Any], phase: str) -> None:
    if phase == "combined_36" and state.get("phase") != "untouched_24_complete":
        raise RuntimeError("untouched_first_violation")
    if state.get("contaminated") and phase in {"untouched_24", "combined_36"}:
        raise RuntimeError("final_holdout_contamination")


def primary_error_attribution(flags: Mapping[str, Any]) -> dict[str, Any]:
    active = [name for name in PRIMARY_ERRORS if bool(flags.get(name))]
    priority = (
        "infrastructure_error",
        "planner_missing_evidence",
        "planner_overselection",
        "critic_missing_evidence",
        "critic_overselection",
        "upstream_provenance_error",
        "adapter_transfer_error",
        "solver_semantic_error",
        "solver_format_error",
        "serialization_error",
    )
    primary = next((name for name in priority if name in active), None)
    return {"primary": primary, "secondary": [name for name in active if name != primary]}


def final_technical_gate(aggregates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    candidate = aggregates["D_recursive_trained"]
    baseline = aggregates["B_qwen3_direct"]
    fresh = aggregates["C_recursive_fresh"]
    absolute = bool(
        int(candidate["semantic_complete_count"]) >= 21
        and int(candidate["schema_valid_count"]) >= 22
        and float(candidate["rule_recall"]) >= 0.875
        and float(candidate["source_recall"]) >= 0.875
        and float(candidate["contradiction_recall"]) >= 0.90
        and float(candidate["counterargument_coverage"]) >= 0.90
        and float(candidate["uncertainty_presence"]) >= 0.90
        and float(candidate["recommendation_presence"]) == 1.0
        and int(candidate["invented_rule_ids"]) == 0
        and int(candidate["invented_source_ids"]) == 0
        and int(candidate["safety_violations"]) == 0
        and int(candidate["approval_violations"]) == 0
    )
    gains = {
        metric: _relative_gain(float(candidate[metric]), float(baseline[metric]))
        for metric in FINAL_QUALITATIVE_METRICS
    }
    deltas = {
        metric: float(candidate[metric]) - float(baseline[metric])
        for metric in ("schema_validity", "rule_recall", "source_recall")
    }
    baseline_pass = max(gains.values()) >= 0.10 and min(deltas.values()) >= -0.05
    core = (
        "schema_validity",
        "rule_recall",
        "source_recall",
        *FINAL_QUALITATIVE_METRICS,
    )
    fresh_pass = statistics.mean(float(candidate[key]) for key in core) > statistics.mean(
        float(fresh[key]) for key in core
    )
    passed = absolute and baseline_pass and fresh_pass
    return {
        "passed": passed,
        "classification": "recursive_qwen3_final_holdout_passed" if passed else "recursive_qwen3_final_holdout_failed",
        "absolute_gate": absolute,
        "baseline_gate": baseline_pass,
        "fresh_gate": fresh_pass,
        "relative_gains_vs_B": gains,
        "structural_deltas_vs_B": deltas,
        "planner_refinement_authorized": passed,
        "production_authorized": False,
    }


def _relative_gain(candidate: float, baseline: float) -> float:
    return (candidate - baseline) / baseline if baseline else float(candidate > 0)


def planner_provenance_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    selected_rules = sum(len(set(row["selected_rule_ids"])) for row in rows)
    selected_sources = sum(len(set(row["selected_source_ids"])) for row in rows)
    gold_rules = sum(len(set(row["gold_rule_ids"])) for row in rows)
    gold_sources = sum(len(set(row["gold_source_ids"])) for row in rows)
    rule_hits = sum(len(set(row["selected_rule_ids"]) & set(row["gold_rule_ids"])) for row in rows)
    source_hits = sum(len(set(row["selected_source_ids"]) & set(row["gold_source_ids"])) for row in rows)
    total = len(rows) or 1
    return {
        "rule_recall": rule_hits / gold_rules if gold_rules else 1.0,
        "rule_precision": rule_hits / selected_rules if selected_rules else float(not gold_rules),
        "source_recall": source_hits / gold_sources if gold_sources else 1.0,
        "source_precision": source_hits / selected_sources if selected_sources else float(not gold_sources),
        "mean_selected_rules": selected_rules / total,
        "mean_selected_sources": selected_sources / total,
        "mean_gold_rules": gold_rules / total,
        "mean_gold_sources": gold_sources / total,
        "overselection_rate": ((selected_rules - rule_hits) + (selected_sources - source_hits)) / max(1, selected_rules + selected_sources),
        "missing_evidence_rate": ((gold_rules - rule_hits) + (gold_sources - source_hits)) / max(1, gold_rules + gold_sources),
    }


def timing_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, float]:
    walls = [float(row["timings"]["wall_total_ms"]) for row in rows]
    for row in rows:
        if float(row["timings"]["ttft_ms"]) > float(row["timings"]["wall_total_ms"]):
            raise ValueError("runner_timing_invalid")
    ordered = sorted(walls)
    p95_index = max(0, min(len(ordered) - 1, int(0.95 * len(ordered) + 0.999999) - 1)) if ordered else 0
    return {
        "wall_median_ms": statistics.median(walls) if walls else 0.0,
        "wall_p95_ms": ordered[p95_index] if ordered else 0.0,
        "wall_total_ms": sum(walls),
        "ttft_mean_ms": statistics.mean(float(row["timings"]["ttft_ms"]) for row in rows) if rows else 0.0,
    }


def error_counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    return dict(Counter(str(row["primary_error"]) for row in rows if row.get("primary_error")))
