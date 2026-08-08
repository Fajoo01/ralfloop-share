from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import statistics
import time
from typing import Any, Callable

from .recursive_domain_reasoning_benchmark import ReasoningCase, score_reasoning_output


RUNNERS = (
    "single_qwen_7b_no_domain",
    "single_qwen_7b_with_domain",
    "recursive_mas_math_checkpoint_with_domain_prompts",
    "recursive_mas_domain_reasoning_checkpoint",
)
ADOPTION_METRICS = (
    "contradiction_recall",
    "counterargument_coverage",
    "uncertainty_calibration",
    "recommendation_usefulness",
)


@dataclass(frozen=True)
class BenchmarkDecision:
    adopted: bool
    relative_gains: dict[str, float]
    threshold_met: tuple[str, ...]
    regressions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adopted": self.adopted,
            "relative_gains": self.relative_gains,
            "threshold_met": list(self.threshold_met),
            "regressions": list(self.regressions),
        }


class DomainProfileBenchmark:
    def __init__(self, artifact_root: str | Path) -> None:
        self.artifact_root = Path(artifact_root)

    def run(
        self,
        cases: list[ReasoningCase],
        runners: dict[str, Callable[[ReasoningCase], dict[str, Any]]],
    ) -> dict[str, Any]:
        self.artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        results: dict[str, list[dict[str, Any]]] = {}
        aggregates: dict[str, dict[str, Any]] = {}
        for name in RUNNERS:
            rows: list[dict[str, Any]] = []
            runner = runners.get(name)
            for case in cases:
                started = time.monotonic()
                if runner is None:
                    row = _infrastructure_row(case, "runner_not_configured", started)
                else:
                    try:
                        raw = runner(case)
                        if raw.get("status") == "infrastructure_error":
                            row = _infrastructure_row(case, str(raw.get("reason") or "unknown"), started, raw)
                        else:
                            score = score_reasoning_output(case, raw.get("output"))
                            row = {
                                "case_id": case.id,
                                "category": case.category,
                                "status": "completed",
                                "output": raw.get("output"),
                                "wall_ms": float(raw.get("wall_ms") or (time.monotonic() - started) * 1000.0),
                                "gpu_peak_bytes": raw.get("gpu_peak_bytes"),
                                "ram_peak_bytes": raw.get("ram_peak_bytes"),
                                "native_latent_verified": raw.get("native_latent_verified"),
                                **score,
                            }
                    except Exception as exc:
                        row = _infrastructure_row(case, type(exc).__name__, started)
                rows.append(row)
                self._write_rows(name, rows)
            results[name] = rows
            aggregates[name] = aggregate(rows)
        decision = adoption_decision(aggregates)
        blind, key = anonymize(results)
        (self.artifact_root / "blind_review.json").write_text(json.dumps(blind, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        key_path = self.artifact_root / "anonymization_key.json"
        key_path.write_text(json.dumps(key, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        key_path.chmod(0o600)
        manifest = {
            "case_count": len(cases),
            "runners": list(RUNNERS),
            "aggregates": aggregates,
            "decision": decision.to_dict(),
            "unseen_split": True,
            "codex_is_judge": False,
            "feature_enabled": False,
        }
        (self.artifact_root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return manifest

    def _write_rows(self, name: str, rows: list[dict[str, Any]]) -> None:
        (self.artifact_root / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluable = [row for row in rows if row.get("evaluable")]
    metrics = (
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
        "case_count": len(rows),
        "evaluable_count": len(evaluable),
        "infrastructure_errors": len(rows) - len(evaluable),
        **{name: statistics.mean(float(row[name]) for row in evaluable) if evaluable else None for name in metrics},
        "safety_violations": sum(int(row.get("safety_violations") or 0) for row in evaluable),
        "approval_violations": sum(int(row.get("approval_violations") or 0) for row in evaluable),
        "wall_total_ms": sum(float(row.get("wall_ms") or 0.0) for row in rows),
        "wall_median_ms": statistics.median(float(row.get("wall_ms") or 0.0) for row in rows) if rows else None,
        "gpu_peak_bytes": max([int(row.get("gpu_peak_bytes") or 0) for row in rows] or [0]),
        "ram_peak_bytes": max([int(row.get("ram_peak_bytes") or 0) for row in rows] or [0]),
    }


def adoption_decision(aggregates: dict[str, dict[str, Any]]) -> BenchmarkDecision:
    baseline = aggregates.get("single_qwen_7b_with_domain") or {}
    candidate = aggregates.get("recursive_mas_domain_reasoning_checkpoint") or {}
    if not baseline.get("evaluable_count") or not candidate.get("evaluable_count"):
        return BenchmarkDecision(False, {}, (), ("not_evaluable",))
    gains: dict[str, float] = {}
    threshold_met: list[str] = []
    for name in ADOPTION_METRICS:
        base_value = float(baseline.get(name) or 0.0)
        candidate_value = float(candidate.get(name) or 0.0)
        gain = (candidate_value - base_value) / base_value if base_value else (1.0 if candidate_value > 0 else 0.0)
        gains[name] = gain
        if gain >= 0.10:
            threshold_met.append(name)
    regressions: list[str] = []
    for name in ("schema_validity", "rule_accuracy", "source_accuracy"):
        if float(candidate.get(name) or 0.0) + 1e-12 < float(baseline.get(name) or 0.0):
            regressions.append(name)
    if float(candidate.get("hallucination_rate") or 0.0) > float(baseline.get("hallucination_rate") or 0.0) + 1e-12:
        regressions.append("hallucination_rate")
    if int(candidate.get("safety_violations") or 0):
        regressions.append("safety")
    if int(candidate.get("approval_violations") or 0):
        regressions.append("approval")
    return BenchmarkDecision(bool(threshold_met and not regressions), gains, tuple(threshold_met), tuple(regressions))


def anonymize(results: dict[str, list[dict[str, Any]]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    key = {name: f"candidate_{chr(65 + index)}" for index, name in enumerate(RUNNERS)}
    blind = [
        {"candidate": key[name], "case_id": row.get("case_id"), "output": row.get("output")}
        for name, rows in results.items()
        for row in rows
    ]
    return blind, key


def _infrastructure_row(
    case: ReasoningCase,
    reason: str,
    started: float,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "case_id": case.id,
        "category": case.category,
        "status": "infrastructure_error",
        "reason": reason,
        "evaluable": False,
        "wall_ms": float((raw or {}).get("wall_ms") or (time.monotonic() - started) * 1000.0),
    }
