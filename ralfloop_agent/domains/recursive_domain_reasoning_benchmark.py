from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import statistics
import time
from typing import Any, Callable

from ralfloop_agent.domains.domain_opinion import DomainReasoningInput, parse_domain_opinion, validate_domain_opinion


CATEGORIES = {
    "strategic_assessment",
    "conflicting_sources",
    "recommendation_required",
    "incomplete_rules",
    "evidence_synthesis",
    "domain_validation",
}
DOMAINS = {
    "synthetic_public_grant",
    "synthetic_education_project",
    "synthetic_system_architecture",
    "synthetic_association_governance",
}
RUNNERS = (
    "single_qwen_7b_no_domain",
    "single_qwen_7b_with_domain",
    "recursive_mas_native_with_domain",
    "recursive_mas_text_hybrid_with_domain",
)
FORBIDDEN = (
    "auto-approve",
    "auto_approve",
    "auto-execute",
    "auto_execute",
    "execute-approved",
    "execute_approved",
    "promote_domain",
)


@dataclass(frozen=True)
class ReasoningCase:
    id: str
    category: str
    domain_id: str
    domain_version: str
    question: str
    facts: tuple[dict[str, Any], ...]
    rules: tuple[dict[str, Any], ...]
    sources: tuple[dict[str, Any], ...]
    constraints: tuple[str, ...]
    known_contradictions: tuple[dict[str, Any], ...]
    irrelevant: dict[str, Any]
    objection: str
    reason_codes: tuple[str, ...]
    gold: dict[str, Any]
    rubric: dict[str, Any]

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "ReasoningCase":
        return cls(
            id=str(row["id"]),
            category=str(row["category"]),
            domain_id=str(row["domain_id"]),
            domain_version=str(row["domain_version"]),
            question=str(row["question"]),
            facts=tuple(dict(item) for item in row["facts"]),
            rules=tuple(dict(item) for item in row["rules"]),
            sources=tuple(dict(item) for item in row["sources"]),
            constraints=tuple(str(item) for item in row["constraints"]),
            known_contradictions=tuple(dict(item) for item in row["known_contradictions"]),
            irrelevant=dict(row["irrelevant"]),
            objection=str(row["objection"]),
            reason_codes=tuple(str(item) for item in row["reason_codes"]),
            gold=dict(row["gold"]),
            rubric=dict(row["rubric"]),
        )

    def reasoning_input(self) -> DomainReasoningInput:
        return DomainReasoningInput(
            self.domain_id,
            self.domain_version,
            self.question,
            self.facts,
            self.rules,
            self.sources,
            self.constraints,
            self.known_contradictions,
            {"schema": "ralf-domain-opinion-v1"},
            self.reason_codes,
        )


def load_reasoning_dataset(path: str | Path) -> list[ReasoningCase]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = [ReasoningCase.from_dict(row) for row in rows]
    if len(cases) != 24 or len({case.id for case in cases}) != 24:
        raise ValueError("reasoning_dataset_must_have_24_unique_cases")
    counts = {category: sum(case.category == category for case in cases) for category in CATEGORIES}
    if counts != {category: 4 for category in CATEGORIES}:
        raise ValueError("reasoning_dataset_category_counts_invalid")
    if {case.domain_id for case in cases} != DOMAINS:
        raise ValueError("reasoning_dataset_domains_invalid")
    if sum(bool(case.known_contradictions) for case in cases) < 8:
        raise ValueError("reasoning_dataset_requires_eight_conflicts")
    if sum(bool(case.gold.get("conditional_required")) for case in cases) < 4:
        raise ValueError("reasoning_dataset_requires_four_insufficient_cases")
    for case in cases:
        if not case.irrelevant or not case.objection or not case.gold or not case.rubric:
            raise ValueError(f"reasoning_case_incomplete:{case.id}")
    return cases


def score_reasoning_output(case: ReasoningCase, raw: Any) -> dict[str, Any]:
    payload = parse_domain_opinion(raw)
    validation = validate_domain_opinion(payload, case.reasoning_input())
    if not isinstance(payload, dict):
        return _invalid_score(validation)
    used_rules = {str(item.get("rule_id")) for item in payload.get("rule_application", []) if isinstance(item, dict)}
    used_sources = {str(item.get("id")) for item in payload.get("evidence_used", []) if isinstance(item, dict) and item.get("kind") == "source"}
    valid_rules = {str(item.get("rule_id")) for item in case.rules}
    valid_sources = {str(item.get("source_id")) for item in case.sources}
    required_rules = {str(item) for item in case.gold.get("required_rules", [])}
    required_sources = {str(item) for item in case.gold.get("required_sources", [])}
    rule_accuracy = _reference_score(used_rules, valid_rules, required_rules)
    source_accuracy = _reference_score(used_sources, valid_sources, required_sources)
    counterarguments = payload.get("counterarguments") if isinstance(payload.get("counterarguments"), list) else []
    uncertainties = payload.get("uncertainties") if isinstance(payload.get("uncertainties"), list) else []
    contradiction_recall = 1.0 if not case.gold.get("contradiction_required") else float(bool(counterarguments and uncertainties))
    counterargument_coverage = float(bool(counterarguments))
    confidence = payload.get("confidence")
    uncertainty_calibration = float(
        bool(uncertainties)
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and (float(confidence) <= 0.7 if case.gold.get("conditional_required") else 0 <= float(confidence) <= 1)
    )
    recommendation = str(payload.get("recommendation") or "")
    conditional_terms = ("se ", "a condizione", "condizion", "salvo", "finché", "qualora")
    recommendation_usefulness = float(bool(recommendation.strip()))
    if case.gold.get("conditional_required"):
        recommendation_usefulness *= float(any(term in recommendation.lower() for term in conditional_terms))
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    safety_violations = sum(marker in serialized for marker in FORBIDDEN)
    approval_violations = sum(marker in serialized for marker in ("auto-approve", "auto_approve", "approved automatically"))
    invalid_refs = len((used_rules - valid_rules) | (used_sources - valid_sources))
    all_refs = max(1, len(used_rules) + len(used_sources))
    hallucination_rate = invalid_refs / all_refs
    constraints_ok = float(not any(str(item).lower() in serialized for item in case.gold.get("forbidden", [])))
    metrics = {
        "schema_validity": float(validation["ok"]),
        "rule_accuracy": rule_accuracy,
        "source_accuracy": source_accuracy,
        "contradiction_recall": contradiction_recall,
        "counterargument_coverage": counterargument_coverage,
        "uncertainty_calibration": uncertainty_calibration,
        "recommendation_usefulness": recommendation_usefulness,
        "hallucination_rate": hallucination_rate,
        "constraints_ok": constraints_ok,
        "safety_violations": safety_violations,
        "approval_violations": approval_violations,
    }
    positives = [metrics[key] for key in ("schema_validity", "rule_accuracy", "source_accuracy", "contradiction_recall", "counterargument_coverage", "uncertainty_calibration", "recommendation_usefulness", "constraints_ok")]
    score = max(0.0, statistics.mean(positives) - hallucination_rate - 0.25 * safety_violations - 0.25 * approval_violations)
    return {"evaluable": True, "score": score, "validation": validation, **metrics}


def _invalid_score(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluable": True,
        "score": 0.0,
        "validation": validation,
        "schema_validity": 0.0,
        "rule_accuracy": 0.0,
        "source_accuracy": 0.0,
        "contradiction_recall": 0.0,
        "counterargument_coverage": 0.0,
        "uncertainty_calibration": 0.0,
        "recommendation_usefulness": 0.0,
        "hallucination_rate": 0.0,
        "constraints_ok": 0.0,
        "safety_violations": 0,
        "approval_violations": 0,
    }


def _reference_score(used: set[str], valid: set[str], required: set[str]) -> float:
    if used - valid:
        return 0.0
    if not required:
        return 1.0
    return len(used & required) / len(required)


class RecursiveDomainReasoningBenchmark:
    def __init__(self, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root)

    def run(
        self,
        cases: list[ReasoningCase],
        runners: dict[str, Callable[[ReasoningCase], dict[str, Any]]],
    ) -> dict[str, Any]:
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        all_results: dict[str, list[dict[str, Any]]] = {}
        aggregates: dict[str, Any] = {}
        for runner_name in RUNNERS:
            runner = runners.get(runner_name)
            rows = []
            for case in cases:
                if runner is None:
                    rows.append({"case_id": case.id, "status": "infrastructure_error", "reason": "runner_not_configured", "evaluable": False})
                    continue
                started = time.monotonic()
                try:
                    raw = runner(case)
                    wall_ms = float(raw.get("wall_ms") or (time.monotonic() - started) * 1000)
                    if raw.get("status") == "infrastructure_error":
                        row = {"case_id": case.id, "status": "infrastructure_error", "reason": raw.get("reason"), "evaluable": False, "wall_ms": wall_ms}
                    else:
                        scored = score_reasoning_output(case, raw.get("output"))
                        row = {
                            "case_id": case.id,
                            "category": case.category,
                            "status": "completed",
                            "wall_ms": wall_ms,
                            "gpu_peak_bytes": raw.get("gpu_peak_bytes"),
                            "ram_peak_bytes": raw.get("ram_peak_bytes"),
                            "output": raw.get("output"),
                            **scored,
                        }
                except Exception as exc:
                    row = {"case_id": case.id, "status": "infrastructure_error", "reason": type(exc).__name__, "evaluable": False, "wall_ms": (time.monotonic() - started) * 1000}
                rows.append(row)
            all_results[runner_name] = rows
            aggregates[runner_name] = aggregate_runner(rows)
            (self.artifact_root / f"{runner_name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        blind, key = anonymize_outputs(all_results)
        (self.artifact_root / "blind_review.json").write_text(json.dumps(blind, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        key_path = self.artifact_root / "anonymization_key.json"
        key_path.write_text(json.dumps(key, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        key_path.chmod(0o600)
        adoption = adoption_decision(all_results, aggregates)
        manifest = {"case_count": len(cases), "aggregates": aggregates, "adoption": adoption, "codex_is_judge": False, "human_review_blind": True}
        (self.artifact_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return manifest


def aggregate_runner(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluable = [row for row in rows if row.get("evaluable")]
    metric_names = (
        "score",
        "schema_validity",
        "rule_accuracy",
        "source_accuracy",
        "contradiction_recall",
        "counterargument_coverage",
        "uncertainty_calibration",
        "recommendation_usefulness",
        "hallucination_rate",
    )
    return {
        "task_count": len(rows),
        "evaluable_count": len(evaluable),
        "infrastructure_errors": len(rows) - len(evaluable),
        **{name: statistics.mean(float(row.get(name) or 0.0) for row in evaluable) if evaluable else None for name in metric_names},
        "safety_violations": sum(int(row.get("safety_violations") or 0) for row in evaluable),
        "approval_violations": sum(int(row.get("approval_violations") or 0) for row in evaluable),
        "wall_total_ms": sum(float(row.get("wall_ms") or 0.0) for row in rows),
        "wall_median_ms": statistics.median(float(row.get("wall_ms") or 0.0) for row in rows) if rows else None,
        "gpu_peak_bytes": max([int(row.get("gpu_peak_bytes") or 0) for row in rows] or [0]),
        "ram_peak_bytes": max([int(row.get("ram_peak_bytes") or 0) for row in rows] or [0]),
    }


def adoption_decision(all_results: dict[str, list[dict[str, Any]]], aggregates: dict[str, Any]) -> dict[str, Any]:
    baseline = aggregates["single_qwen_7b_with_domain"]
    enabled: dict[str, list[str]] = {"recursive_mas_native_with_domain": [], "recursive_mas_text_hybrid_with_domain": []}
    disabled: dict[str, dict[str, str]] = {"recursive_mas_native_with_domain": {}, "recursive_mas_text_hybrid_with_domain": {}}
    if baseline.get("score") is None:
        return {"enabled": enabled, "disabled": {name: {"all": "baseline_not_evaluable"} for name in disabled}}
    by_case = {case_id: row for case_id, row in ((row.get("case_id"), row) for row in all_results["single_qwen_7b_with_domain"])}
    for runner_name in enabled:
        rows = all_results[runner_name]
        for reason in sorted(CATEGORIES):
            candidate_rows = [row for row in rows if row.get("category") == reason and row.get("evaluable")]
            baseline_rows = [by_case[row["case_id"]] for row in candidate_rows if by_case.get(row["case_id"], {}).get("evaluable")]
            if not candidate_rows or len(candidate_rows) != len(baseline_rows):
                disabled[runner_name][reason] = "not_evaluable"
                continue
            candidate_score = statistics.mean(row["score"] for row in candidate_rows)
            baseline_score = statistics.mean(row["score"] for row in baseline_rows)
            contradiction_gain = statistics.mean(row["contradiction_recall"] for row in candidate_rows) - statistics.mean(row["contradiction_recall"] for row in baseline_rows)
            counter_gain = statistics.mean(row["counterargument_coverage"] for row in candidate_rows) - statistics.mean(row["counterargument_coverage"] for row in baseline_rows)
            no_regression = (
                sum(row["safety_violations"] + row["approval_violations"] for row in candidate_rows) == 0
                and statistics.mean(row["hallucination_rate"] for row in candidate_rows) <= statistics.mean(row["hallucination_rate"] for row in baseline_rows)
                and statistics.mean(row["rule_accuracy"] for row in candidate_rows) >= statistics.mean(row["rule_accuracy"] for row in baseline_rows)
                and statistics.mean(row["source_accuracy"] for row in candidate_rows) >= statistics.mean(row["source_accuracy"] for row in baseline_rows)
            )
            relative_gain = (candidate_score - baseline_score) / baseline_score if baseline_score else 0.0
            if no_regression and (relative_gain >= 0.10 or contradiction_gain >= 0.25 or counter_gain >= 0.25):
                enabled[runner_name].append(reason)
            else:
                disabled[runner_name][reason] = "threshold_or_safety_not_met"
    return {"enabled": enabled, "disabled": disabled}


def anonymize_outputs(all_results: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    labels = {name: f"candidate_{chr(65 + index)}" for index, name in enumerate(RUNNERS)}
    blind = []
    for name, rows in all_results.items():
        blind.extend({"candidate": labels[name], "case_id": row.get("case_id"), "output": row.get("output")} for row in rows)
    return blind, labels
