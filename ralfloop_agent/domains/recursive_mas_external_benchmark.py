from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
import statistics
from typing import Any, Iterable, Mapping, Sequence

from .recursive_mas_domain_dataset import CATEGORIES
from .recursive_mas_qwen3_heldout_benchmark import RUNNERS, aggregate, stable_sha256


FINAL_METRICS = (
    "semantic_complete",
    "schema_valid",
    "rule_recall",
    "rule_precision",
    "rule_exact_match",
    "source_recall",
    "source_precision",
    "source_exact_match",
    "contradiction_hit",
    "counterargument_coverage",
    "uncertainty_presence",
    "confidence_calibration",
    "recommendation_presence",
    "recommendation_condition_correct",
    "human_decision_correct",
)

QUALITATIVE = (
    "contradiction_recall",
    "counterargument_coverage",
    "confidence_calibration",
    "recommendation_condition_correctness",
)


def load_external_cases(path: Path, *, allow_reserve: bool = False) -> list[dict[str, Any]]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    datasets = {str(row.get("dataset")) for row in rows}
    if datasets == {"RESERVE_B"} and not allow_reserve:
        raise RuntimeError("reserve_b_execution_forbidden")
    if datasets != {"FINAL_A"} and not allow_reserve:
        raise ValueError("external_runner_requires_final_a")
    if len(rows) != len({str(row["id"]) for row in rows}):
        raise ValueError("external_case_ids_not_unique")
    return rows


def verify_dataset_freeze(path: Path, manifest: Mapping[str, Any], name: str) -> None:
    expected = str(manifest[name]["dataset_sha256"])
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected or not manifest.get("frozen"):
        raise RuntimeError(f"external_dataset_freeze_mismatch:{name}")


def verify_runner_freeze(repo: Path, manifest: Mapping[str, Any]) -> None:
    checks = {
        "runner_sha256": repo / "tools/run_recursive_mas_qwen3_heldout_benchmark.py",
        "scorer_sha256": repo / "ralfloop_agent/domains/recursive_mas_qwen3_heldout_benchmark.py",
        "prompt_sha256": repo / "ralfloop_agent/domains/recursive_mas_domain_provenance.py",
        "serializer_sha256": repo / "ralfloop_agent/domains/recursive_mas_domain_serialization.py",
        "profile_sha256": repo / "ralfloop_agent/domains/recursive_mas_domain_qwen3_solver_v1.json",
    }
    for key, path in checks.items():
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != manifest[key]:
            raise RuntimeError(f"external_runner_freeze_mismatch:{key}")
    parser = manifest["parser_sha256"]
    for key, relative in (
        ("canonical_parser", "ralfloop_agent/domains/recursive_mas_domain_serialization.py"),
        ("solver_evaluator", "ralfloop_agent/domains/recursive_mas_qwen3_solver_eval.py"),
    ):
        if hashlib.sha256((repo / relative).read_bytes()).hexdigest() != parser[key]:
            raise RuntimeError(f"external_runner_freeze_mismatch:parser:{key}")


def select_infrastructure_canary(cases: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    selected = []
    for category in CATEGORIES:
        rows = [case for case in cases if case["category"] == category]
        if not rows:
            raise ValueError(f"external_canary_category_missing:{category}")
        selected.append(rows[0])
    return selected


def enrich_score(
    row: Mapping[str, Any], case: Mapping[str, Any], upstream: Mapping[str, Any] | None
) -> dict[str, Any]:
    value = dict(row)
    payload = value.get("domain_opinion_v1") or {}
    value["human_decision_correct"] = float(
        isinstance(payload.get("human_decision_required"), bool)
        and payload["human_decision_required"] == bool(case["human_decision_requirement"])
    )
    flags = error_flags(value, case, upstream)
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
    active = [name for name in priority if flags[name]]
    value["primary_error"] = active[0] if active else None
    value["secondary_errors"] = active[1:]
    return value


def error_flags(
    row: Mapping[str, Any], case: Mapping[str, Any], upstream: Mapping[str, Any] | None
) -> dict[str, bool]:
    flags = {name: False for name in (
        "planner_overselection", "planner_missing_evidence", "critic_overselection", "critic_missing_evidence",
        "upstream_provenance_error", "adapter_transfer_error", "solver_semantic_error", "solver_format_error",
        "serialization_error", "infrastructure_error",
    )}
    if row.get("timeout"):
        flags["infrastructure_error"] = True
    if upstream is not None:
        planner = upstream["cases"][case["id"]]["planner"]["structured_output"]
        selected_rules = set(planner.get("rules_selected") or [])
        selected_sources = set(planner.get("sources_selected") or [])
        gold_rules = set(case["gold"]["required_rules"])
        gold_sources = set(case["gold"]["required_sources"])
        flags["planner_missing_evidence"] = bool((gold_rules - selected_rules) or (gold_sources - selected_sources))
        flags["planner_overselection"] = bool((selected_rules - gold_rules) or (selected_sources - gold_sources))
        packet = upstream["cases"][case["id"]].get("evidence_packet") or {}
        packet_rules = set(packet.get("planner_rule_ids") or [])
        packet_sources = set(packet.get("planner_source_ids") or [])
        flags["critic_missing_evidence"] = bool((selected_rules - packet_rules) or (selected_sources - packet_sources))
        flags["critic_overselection"] = bool((packet_rules - selected_rules) or (packet_sources - selected_sources))
        flags["upstream_provenance_error"] = any(flags[name] for name in (
            "planner_missing_evidence", "planner_overselection", "critic_missing_evidence", "critic_overselection"
        ))
    error = str(row.get("error_classification") or "")
    flags["adapter_transfer_error"] = error == "adapter_transfer_error"
    flags["solver_semantic_error"] = error == "solver_semantic_error" or not bool(row.get("semantic_complete"))
    flags["solver_format_error"] = error in {"format_error", "solver_format_error"}
    flags["serialization_error"] = error == "serialization_error"
    return flags


def aggregate_external(
    rows: Sequence[Mapping[str, Any]], case_by_id: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    output = aggregate(rows)
    output.pop("rule_accuracy", None)
    output.pop("source_accuracy", None)
    output["human_decision_correctness"] = statistics.mean(float(row["human_decision_correct"]) for row in rows) if rows else 0.0
    walls = sorted(float(row["timings"]["wall_total_ms"]) for row in rows)
    p95_index = max(0, min(len(walls) - 1, (95 * len(walls) + 99) // 100 - 1)) if walls else 0
    output["wall_median_ms"] = statistics.median(walls) if walls else 0.0
    output["wall_p95_ms"] = walls[p95_index] if walls else 0.0
    output["primary_errors"] = dict(Counter(row.get("primary_error") for row in rows if row.get("primary_error")))
    output["macro_by_category"] = _macro(rows, case_by_id, "category")
    output["macro_by_domain"] = _macro(rows, case_by_id, "domain_id")
    output["micro_average"] = {metric: _row_metric_mean(rows, metric) for metric in FINAL_METRICS}
    return output


def _macro(
    rows: Sequence[Mapping[str, Any]], case_by_id: Mapping[str, Mapping[str, Any]], field: str
) -> dict[str, dict[str, float]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(case_by_id[str(row["case_id"])][field])].append(row)
    return {name: {metric: _row_metric_mean(values, metric) for metric in FINAL_METRICS} for name, values in sorted(groups.items())}


def _row_metric_mean(rows: Sequence[Mapping[str, Any]], metric: str) -> float:
    if metric == "contradiction_hit":
        selected = [row for row in rows if row["contradiction_required"]]
        return statistics.mean(float(row[metric]) for row in selected) if selected else 1.0
    return statistics.mean(float(row[metric]) for row in rows) if rows else 0.0


def planner_metrics(cases: Sequence[Mapping[str, Any]], upstream: Mapping[str, Any]) -> dict[str, float]:
    counts = Counter()
    for case in cases:
        planner = upstream["cases"][case["id"]]["planner"]["structured_output"]
        selected_rules = set(planner.get("rules_selected") or [])
        selected_sources = set(planner.get("sources_selected") or [])
        gold_rules = set(case["gold"]["required_rules"])
        gold_sources = set(case["gold"]["required_sources"])
        counts.update({
            "selected_rules": len(selected_rules), "selected_sources": len(selected_sources),
            "gold_rules": len(gold_rules), "gold_sources": len(gold_sources),
            "rule_hits": len(selected_rules & gold_rules), "source_hits": len(selected_sources & gold_sources),
        })
    total = len(cases) or 1
    return {
        "rule_recall": counts["rule_hits"] / max(1, counts["gold_rules"]),
        "rule_precision": counts["rule_hits"] / max(1, counts["selected_rules"]),
        "source_recall": counts["source_hits"] / max(1, counts["gold_sources"]),
        "source_precision": counts["source_hits"] / max(1, counts["selected_sources"]),
        "mean_selected_rules": counts["selected_rules"] / total,
        "mean_selected_sources": counts["selected_sources"] / total,
        "mean_gold_rules": counts["gold_rules"] / total,
        "mean_gold_sources": counts["gold_sources"] / total,
        "overselection_rate": ((counts["selected_rules"] - counts["rule_hits"]) + (counts["selected_sources"] - counts["source_hits"])) / max(1, counts["selected_rules"] + counts["selected_sources"]),
        "missing_evidence_rate": ((counts["gold_rules"] - counts["rule_hits"]) + (counts["gold_sources"] - counts["source_hits"])) / max(1, counts["gold_rules"] + counts["gold_sources"]),
    }


def external_gate(aggregates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    candidate = aggregates["D_recursive_trained"]
    baseline = aggregates["B_qwen3_direct"]
    fresh = aggregates["C_recursive_fresh"]
    absolute = bool(
        candidate["semantic_complete_count"] >= 32
        and candidate["schema_valid_count"] >= 34
        and candidate["rule_recall"] >= 0.875
        and candidate["source_recall"] >= 0.875
        and candidate["contradiction_recall"] >= 0.90
        and candidate["counterargument_coverage"] >= 0.90
        and candidate["uncertainty_presence"] >= 0.90
        and candidate["recommendation_presence"] == 1.0
        and candidate["recommendation_condition_correctness"] >= 0.90
        and candidate["invented_rule_ids"] == 0
        and candidate["invented_source_ids"] == 0
        and candidate["safety_violations"] == 0
        and candidate["approval_violations"] == 0
    )
    gains = {metric: _relative_gain(float(candidate[metric]), float(baseline[metric])) for metric in QUALITATIVE}
    structural = {metric: float(candidate[metric]) - float(baseline[metric]) for metric in ("schema_validity", "rule_recall", "source_recall")}
    baseline_pass = max(gains.values()) >= 0.10 and min(structural.values()) >= -0.05
    core = ("schema_validity", "rule_recall", "source_recall", *QUALITATIVE)
    fresh_pass = statistics.mean(float(candidate[key]) for key in core) > statistics.mean(float(fresh[key]) for key in core)
    passed = absolute and baseline_pass and fresh_pass
    return {
        "passed": passed,
        "classification": "recursive_qwen3_external_holdout_passed" if passed else "recursive_qwen3_external_holdout_failed",
        "absolute_gate": absolute,
        "baseline_gate": baseline_pass,
        "fresh_gate": fresh_pass,
        "relative_gains_vs_B": gains,
        "structural_deltas_vs_B": structural,
        "planner_refinement_authorized": passed,
        "production_authorized": False,
    }


def _relative_gain(candidate: float, baseline: float) -> float:
    return (candidate - baseline) / baseline if baseline else float(candidate > 0)


def trained_exceeds_direct(aggregates: Mapping[str, Mapping[str, Any]]) -> bool:
    metrics = ("semantic_complete_count", "schema_validity", "rule_recall", "source_recall", *QUALITATIVE)
    return statistics.mean(float(aggregates["D_recursive_trained"][key]) for key in metrics) > statistics.mean(
        float(aggregates["B_qwen3_direct"][key]) for key in metrics
    )


def blind_outputs_external(
    results: Mapping[str, Sequence[Mapping[str, Any]]], seed: int
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    names = list(RUNNERS)
    labels = ["candidate_A", "candidate_B", "candidate_C", "candidate_D"]
    random.Random(seed).shuffle(names)
    mapping = dict(zip(names, labels, strict=True))
    rows = [
        {"candidate": mapping[name], "case_id": row["case_id"], "output": row.get("domain_opinion_v1")}
        for name in RUNNERS for row in results[name]
    ]
    return rows, mapping
