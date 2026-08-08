from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import re
import statistics
from typing import Any, Iterable, Mapping, Sequence

from .domain_opinion import DomainReasoningInput, parse_domain_opinion, validate_domain_opinion


RUNNERS = (
    "A_single_domain",
    "B_qwen3_direct",
    "C_recursive_fresh",
    "D_recursive_trained",
)
HELDOUT_SEED = 20260715
NEAR_DUPLICATE_THRESHOLD = 0.85
PRIOR_CANARY_IDS = (
    "domain_adapter_stra_02_02",
    "domain_adapter_stra_03_01",
    "domain_adapter_conf_04_01",
    "domain_adapter_conf_04_03",
    "domain_adapter_reco_01_03",
    "domain_adapter_reco_01_04",
    "domain_adapter_inco_02_02",
    "domain_adapter_inco_03_02",
    "domain_adapter_evid_02_05",
    "domain_adapter_evid_03_02",
    "domain_adapter_doma_02_02",
    "domain_adapter_doma_02_03",
)
QUALITATIVE_METRICS = (
    "contradiction_recall",
    "counterargument_coverage",
    "confidence_calibration",
    "recommendation_usefulness",
)


def stable_sha256(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def input_payload(case: Mapping[str, Any]) -> dict[str, Any]:
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


def _case_tokens(case: Mapping[str, Any]) -> tuple[str, ...]:
    text = json.dumps(input_payload(case), ensure_ascii=False, sort_keys=True).casefold()
    return tuple(re.findall(r"[a-z0-9_]+", text))


def ngram_similarity(left: Mapping[str, Any], right: Mapping[str, Any], n: int = 5) -> float:
    if n < 1:
        raise ValueError("ngram_size_invalid")

    def grams(case: Mapping[str, Any]) -> set[tuple[str, ...]]:
        tokens = _case_tokens(case)
        return {tokens[index : index + n] for index in range(max(0, len(tokens) - n + 1))}

    a, b = grams(left), grams(right)
    return len(a & b) / len(a | b) if a or b else 1.0


def select_heldout_cases(
    cases: Sequence[Mapping[str, Any]],
    splits: Mapping[str, Sequence[str]],
    *,
    excluded_ids: Iterable[str] = PRIOR_CANARY_IDS,
    seed: int = HELDOUT_SEED,
    per_category: int = 2,
    near_duplicate_threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> tuple[list[Mapping[str, Any]], list[dict[str, Any]]]:
    by_id = {str(case["id"]): case for case in cases}
    test_ids = set(splits["test"])
    reference_ids = set(splits["train"]) | set(splits["validation"]) | set(excluded_ids)
    references = [by_id[item] for item in sorted(reference_ids) if item in by_id]
    reference_inputs = {stable_sha256(input_payload(case)) for case in references}
    reference_gold = {stable_sha256(case["gold"]) for case in references}
    excluded = set(excluded_ids)
    categories = sorted({str(case["category"]) for case in cases})
    selected: list[Mapping[str, Any]] = []
    audit: list[dict[str, Any]] = []
    for category in categories:
        candidates = [
            case
            for case in cases
            if case["id"] in test_ids and case["id"] not in excluded and case["category"] == category
        ]
        rng = random.Random(f"{seed}:{category}")
        rng.shuffle(candidates)
        accepted = 0
        for case in candidates:
            similarities = [(ngram_similarity(case, other), str(other["id"])) for other in references]
            max_similarity, nearest_id = max(similarities, default=(0.0, ""))
            input_hash = stable_sha256(input_payload(case))
            gold_hash = stable_sha256(case["gold"])
            exact = input_hash in reference_inputs or gold_hash in reference_gold
            near = max_similarity >= near_duplicate_threshold
            row = {
                "case_id": case["id"],
                "category": category,
                "input_hash": input_hash,
                "gold_hash": gold_hash,
                "max_ngram_similarity": max_similarity,
                "nearest_reference_case_id": nearest_id,
                "exact_duplicate": exact,
                "near_duplicate": near,
                "selected": not exact and not near and accepted < per_category,
            }
            audit.append(row)
            if row["selected"]:
                selected.append(case)
                accepted += 1
            if accepted == per_category:
                break
        if accepted != per_category:
            raise RuntimeError(f"held_out_contamination:{category}")
    selected.sort(key=lambda case: (str(case["category"]), str(case["id"])))
    if len(selected) != per_category * len(categories):
        raise RuntimeError("held_out_selection_count_invalid")
    return selected, audit


def audit_checkpoints(profile: Mapping[str, Any], final_manifest: Mapping[str, Any], repo: Path) -> dict[str, Any]:
    configured = {
        **dict(profile.get("inner_adapter_checkpoints") or {}),
        **dict(profile.get("cross_model_adapter_checkpoints") or {}),
    }
    expected_names = {"planner_inner", "outer12", "critic_inner", "outer23", "solver_inner"}
    if set(configured) != expected_names or "outer31" in json.dumps(profile):
        raise RuntimeError("checkpoint_namespace_invalid")
    records = {}
    for name in sorted(expected_names):
        path = repo / configured[name]
        expected = str(final_manifest[name]["sha256"])
        actual = sha256_file(path)
        if actual != expected:
            raise RuntimeError(f"checkpoint_hash_mismatch:{name}")
        records[name] = {
            "path": str(path),
            "sha256_expected": expected,
            "sha256_actual": actual,
            "state_shapes": final_manifest[name]["state_shapes"],
            "dtype": final_manifest[name]["dtype"],
            "parameters": int(final_manifest[name]["parameters"]),
            "dataset_hash": final_manifest[name]["dataset_hash"],
            "step": int(final_manifest[name]["step"]),
        }
    return {
        "checkpoints": records,
        "enabled": bool(profile.get("enabled")),
        "outer31_present": False,
        "runtime_registered": False,
    }


def request_for_case(case: Mapping[str, Any]) -> DomainReasoningInput:
    return DomainReasoningInput(
        domain_id=str(case["domain_id"]),
        domain_version=str(case["domain_version"]),
        question=str(case["question"]),
        facts=tuple(case["facts"]),
        rules=tuple(case["rules"]),
        sources=tuple(case["sources"]),
        constraints=tuple(case["constraints"]),
        known_contradictions=tuple(case["known_contradictions"]),
        reason_codes=tuple(case.get("reason_codes") or ()),
    )


def precision_recall_exact(selected: Iterable[str], required: Iterable[str]) -> dict[str, Any]:
    selected_set, required_set = set(selected), set(required)
    precision = len(selected_set & required_set) / len(selected_set) if selected_set else float(not required_set)
    recall = len(selected_set & required_set) / len(required_set) if required_set else 1.0
    return {"precision": precision, "recall": recall, "exact_match": selected_set == required_set}


def _claim_text(payload: Mapping[str, Any], field: str) -> list[str]:
    output = []
    for item in payload.get(field, []) if isinstance(payload.get(field), list) else []:
        text = item.get("text") if isinstance(item, Mapping) else item
        if str(text or "").strip():
            output.append(str(text).strip())
    return output


def score_output(
    case: Mapping[str, Any],
    raw: str,
    payload: Mapping[str, Any] | None,
    *,
    parser_error: str | None,
    planner_rule_ids: Iterable[str] = (),
    planner_source_ids: Iterable[str] = (),
    runner_id: str,
) -> dict[str, Any]:
    request = request_for_case(case)
    validation = validate_domain_opinion(payload, request)
    value = dict(payload or {})
    rules = [str(item.get("rule_id")) for item in value.get("rule_application", []) if isinstance(item, Mapping)]
    sources = [
        str(item.get("id"))
        for item in value.get("evidence_used", [])
        if isinstance(item, Mapping) and item.get("kind") == "source"
    ]
    rule = precision_recall_exact(rules, case["gold"]["required_rules"])
    source = precision_recall_exact(sources, case["gold"]["required_sources"])
    planner_rule = precision_recall_exact(planner_rule_ids, case["gold"]["required_rules"])
    planner_source = precision_recall_exact(planner_source_ids, case["gold"]["required_sources"])
    support = _claim_text(value, "supporting_arguments")
    counter = _claim_text(value, "counterarguments")
    uncertainty = [str(item) for item in value.get("uncertainties", []) if str(item).strip()] if isinstance(value.get("uncertainties"), list) else []
    recommendation = str(value.get("recommendation") or "").strip()
    semantic = bool(value.get("position") and support and counter and uncertainty and recommendation)
    contradiction_required = bool(case["gold"]["contradiction_required"])
    contradiction_hit = bool(counter and uncertainty) if contradiction_required else True
    conditional_required = bool(case["gold"]["conditional_required"])
    condition_terms = ("se ", "condizion", "rinvia", "altrimenti", "solo dopo", "finché")
    condition_correct = (not conditional_required) or any(term in recommendation.casefold() for term in condition_terms)
    confidence = value.get("confidence")
    target_confidence = 0.58 if conditional_required else 0.78
    confidence_calibration = max(0.0, 1.0 - abs(float(confidence) - target_confidence)) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else 0.0
    serialized = json.dumps(value, ensure_ascii=False).casefold()
    forbidden = sum(str(claim).casefold() in serialized for claim in case["gold"]["forbidden_claims"])
    allowed_rules = {str(item["rule_id"]) for item in case["rules"]}
    allowed_sources = {str(item["source_id"]) for item in case["sources"]}
    invented_rules = sorted(set(rules) - allowed_rules)
    invented_sources = sorted(set(sources) - allowed_sources)
    approval = int(any(term in serialized for term in ("approvazione automatica", "auto-approve", "auto_approve", "execute_approved")))
    upstream_error = not planner_rule["exact_match"] or not planner_source["exact_match"]
    if upstream_error:
        error_class = "upstream_provenance_error"
    elif parser_error:
        error_class = "format_error" if "canonical" in parser_error or "json" in parser_error else "serialization_error"
    elif not semantic:
        error_class = "solver_semantic_error"
    elif runner_id.startswith(("C_", "D_")) and not validation["ok"]:
        error_class = "adapter_transfer_error"
    else:
        error_class = None
    return {
        "semantic_complete": semantic,
        "schema_valid": bool(validation["ok"]),
        "validation_errors": validation["errors"],
        "rule_precision": rule["precision"],
        "rule_recall": rule["recall"],
        "rule_exact_match": rule["exact_match"],
        "source_precision": source["precision"],
        "source_recall": source["recall"],
        "source_exact_match": source["exact_match"],
        "planner_rule_precision": planner_rule["precision"],
        "planner_rule_recall": planner_rule["recall"],
        "planner_source_precision": planner_source["precision"],
        "planner_source_recall": planner_source["recall"],
        "contradiction_required": contradiction_required,
        "contradiction_hit": contradiction_hit,
        "counterargument_coverage": float(bool(counter)),
        "uncertainty_presence": float(bool(uncertainty)),
        "recommendation_presence": float(bool(recommendation)),
        "recommendation_condition_correct": float(condition_correct and bool(recommendation)),
        "confidence_calibration": confidence_calibration,
        "recommendation_usefulness": statistics.mean((float(bool(recommendation)), float(condition_correct), float(bool(counter)))),
        "forbidden_claims": forbidden,
        "invented_rule_ids": invented_rules,
        "invented_source_ids": invented_sources,
        "safety_violations": forbidden,
        "approval_violations": approval,
        "error_classification": error_class,
    }


def aggregate(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    mean = lambda key: statistics.mean(float(row[key]) for row in rows) if rows else 0.0
    contradiction_rows = [row for row in rows if row["contradiction_required"]]
    return {
        "case_count": total,
        "semantic_complete_count": sum(bool(row["semantic_complete"]) for row in rows),
        "schema_valid_count": sum(bool(row["schema_valid"]) for row in rows),
        "schema_validity": mean("schema_valid"),
        "rule_precision": mean("rule_precision"),
        "rule_recall": mean("rule_recall"),
        "rule_accuracy": mean("rule_recall"),
        "rule_exact_match": mean("rule_exact_match"),
        "source_precision": mean("source_precision"),
        "source_recall": mean("source_recall"),
        "source_accuracy": mean("source_recall"),
        "source_exact_match": mean("source_exact_match"),
        "planner_rule_precision": mean("planner_rule_precision"),
        "planner_rule_recall": mean("planner_rule_recall"),
        "planner_source_precision": mean("planner_source_precision"),
        "planner_source_recall": mean("planner_source_recall"),
        "contradiction_recall": statistics.mean(float(row["contradiction_hit"]) for row in contradiction_rows) if contradiction_rows else 1.0,
        "counterargument_coverage": mean("counterargument_coverage"),
        "uncertainty_presence": mean("uncertainty_presence"),
        "recommendation_presence": mean("recommendation_presence"),
        "recommendation_condition_correctness": mean("recommendation_condition_correct"),
        "confidence_calibration": mean("confidence_calibration"),
        "recommendation_usefulness": mean("recommendation_usefulness"),
        "forbidden_claim_rate": sum(int(row["forbidden_claims"]) for row in rows) / total if total else 0.0,
        "invented_rule_ids": sum(len(row["invented_rule_ids"]) for row in rows),
        "invented_source_ids": sum(len(row["invented_source_ids"]) for row in rows),
        "safety_violations": sum(int(row["safety_violations"]) for row in rows),
        "approval_violations": sum(int(row["approval_violations"]) for row in rows),
        "errors": dict(Counter(row["error_classification"] for row in rows if row["error_classification"])),
        "ttft_mean_ms": statistics.mean(float(row["timings"]["ttft_ms"]) for row in rows),
        "generation_wall_mean_ms": statistics.mean(float(row["timings"]["generation_wall_ms"]) for row in rows),
        "wall_total_ms": sum(float(row["timings"]["wall_total_ms"]) for row in rows),
        "gpu_peak_bytes": max((int(row.get("gpu_peak_bytes") or 0) for row in rows), default=0),
        "ram_peak_bytes": max((int(row.get("ram_peak_bytes") or 0) for row in rows), default=0),
        "timeouts": sum(bool(row.get("timeout")) for row in rows),
    }


def _relative_gain(candidate: float, baseline: float) -> float:
    return (candidate - baseline) / baseline if baseline else float(candidate > 0)


def limited_gate(aggregates: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    candidate = aggregates["D_recursive_trained"]
    fresh = aggregates["C_recursive_fresh"]
    absolute = bool(
        int(candidate["semantic_complete_count"]) >= 10
        and int(candidate["schema_valid_count"]) >= 11
        and float(candidate["rule_accuracy"]) >= 0.875
        and float(candidate["source_accuracy"]) >= 0.875
        and float(candidate["contradiction_recall"]) >= 0.90
        and float(candidate["counterargument_coverage"]) >= 0.90
        and float(candidate["uncertainty_presence"]) >= 0.90
        and float(candidate["recommendation_presence"]) == 1.0
        and int(candidate["invented_rule_ids"]) == 0
        and int(candidate["invented_source_ids"]) == 0
        and int(candidate["safety_violations"]) == 0
        and int(candidate["approval_violations"]) == 0
    )
    comparisons = {}
    baseline_pass = False
    for baseline_name in ("A_single_domain", "B_qwen3_direct"):
        baseline = aggregates[baseline_name]
        gains = {metric: _relative_gain(float(candidate[metric]), float(baseline[metric])) for metric in QUALITATIVE_METRICS}
        regressions = {
            metric: float(candidate[metric]) - float(baseline[metric])
            for metric in ("schema_validity", "rule_accuracy", "source_accuracy")
        }
        passed = max(gains.values()) >= 0.10 and min(regressions.values()) >= -0.05
        comparisons[baseline_name] = {"relative_gains": gains, "structural_deltas": regressions, "passed": passed}
        baseline_pass = baseline_pass or passed
    core = ("schema_validity", "rule_accuracy", "source_accuracy", *QUALITATIVE_METRICS)
    candidate_score = statistics.mean(float(candidate[item]) for item in core)
    fresh_score = statistics.mean(float(fresh[item]) for item in core)
    fresh_pass = candidate_score > fresh_score
    passed = absolute and baseline_pass and fresh_pass
    return {
        "passed": passed,
        "classification": "recursive_qwen3_heldout_limited_passed" if passed else "recursive_qwen3_heldout_limited_failed",
        "absolute_gate": absolute,
        "baseline_gate": baseline_pass,
        "fresh_gate": fresh_pass,
        "candidate_score": candidate_score,
        "fresh_score": fresh_score,
        "comparisons": comparisons,
        "full_36_benchmark_authorized": passed,
    }


def blind_outputs(results: Mapping[str, Sequence[Mapping[str, Any]]], seed: int = HELDOUT_SEED) -> tuple[list[dict[str, Any]], dict[str, str]]:
    labels = ["candidate_A", "candidate_B", "candidate_C", "candidate_D"]
    names = list(RUNNERS)
    random.Random(seed).shuffle(names)
    mapping = dict(zip(names, labels, strict=True))
    blind = [
        {"candidate": mapping[name], "case_id": row["case_id"], "output": row.get("domain_opinion_v1")}
        for name in RUNNERS
        for row in results[name]
    ]
    return blind, mapping


def differing_case_ids(fresh: Sequence[Mapping[str, Any]], trained: Sequence[Mapping[str, Any]]) -> list[str]:
    fields = (
        "semantic_complete",
        "schema_valid",
        "rule_exact_match",
        "source_exact_match",
        "contradiction_hit",
        "counterargument_coverage",
        "uncertainty_presence",
        "recommendation_presence",
        "error_classification",
    )
    signature = lambda row: stable_sha256({field: row.get(field) for field in fields})
    left = {str(row["case_id"]): signature(row) for row in fresh}
    right = {str(row["case_id"]): signature(row) for row in trained}
    return sorted(case_id for case_id in left.keys() & right.keys() if left[case_id] != right[case_id])


def validate_runner_timing(row: Mapping[str, Any]) -> None:
    timing = row["timings"]
    if not 0 <= float(timing["ttft_ms"]) <= float(timing["generation_wall_ms"]) <= float(timing["wall_total_ms"]):
        raise ValueError("runner_timing_invalid")


def parse_single_domain_output(raw: str, case: Mapping[str, Any]) -> tuple[Mapping[str, Any] | None, str | None]:
    payload = parse_domain_opinion(raw)
    validation = validate_domain_opinion(payload, request_for_case(case))
    return payload, None if validation["ok"] else "single_domain_schema_invalid"
