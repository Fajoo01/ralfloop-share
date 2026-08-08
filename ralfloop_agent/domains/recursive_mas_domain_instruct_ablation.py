from __future__ import annotations

from typing import Any, Iterable, Mapping


QUALITY_METRICS = (
    "schema_validity",
    "rule_accuracy",
    "source_accuracy",
    "contradiction_recall",
    "counterargument_coverage",
)
ADOPTION_IMPROVEMENT_METRICS = (
    "contradiction_recall",
    "counterargument_coverage",
    "uncertainty_calibration",
    "recommendation_usefulness",
)
NON_REGRESSION_METRICS = (
    "schema_validity",
    "rule_accuracy",
    "source_accuracy",
    "safety",
    "approval_invariants",
)


def progressive_ablation_plan() -> list[dict[str, Any]]:
    common = {"cases": 8, "external_actions": False, "approvals": False}
    return [
        {
            **common,
            "id": "A",
            "name": "solver_instruct_direct_text",
            "introduced_arc": "direct_text_baseline",
            "planner_mode": "gold_text",
            "critic_mode": "gold_text",
            "solver_mode": "direct_text",
        },
        {
            **common,
            "id": "B",
            "name": "solver_instruct_plus_solver_inner",
            "introduced_arc": "solver_inner_to_final_decode",
            "planner_mode": "gold_text",
            "critic_mode": "gold_text",
            "solver_mode": "inner_adapter",
        },
        {
            **common,
            "id": "C",
            "name": "gold_critic_hidden_cross_to_solver",
            "introduced_arc": "outer_23_critic_to_solver",
            "planner_mode": "gold_text",
            "critic_mode": "gold_hidden",
            "solver_mode": "inner_adapter",
        },
        {
            **common,
            "id": "D",
            "name": "planner_direct_critic_direct_solver_direct",
            "introduced_arc": "direct_text_three_stage",
            "planner_mode": "direct_text",
            "critic_mode": "direct_text",
            "solver_mode": "direct_text",
        },
        {
            **common,
            "id": "E",
            "name": "planner_latent_critic_direct_solver_direct",
            "introduced_arc": "planner_inner_outer_12",
            "planner_mode": "latent",
            "critic_mode": "direct_text",
            "solver_mode": "direct_text",
        },
        {
            **common,
            "id": "F",
            "name": "planner_latent_critic_latent_solver_direct",
            "introduced_arc": "critic_inner_outer_23",
            "planner_mode": "latent",
            "critic_mode": "latent",
            "solver_mode": "direct_text",
        },
        {
            **common,
            "id": "G",
            "name": "complete_latent_pipeline",
            "introduced_arc": "solver_inner_final_decode",
            "planner_mode": "latent",
            "critic_mode": "latent",
            "solver_mode": "latent_final_decode",
        },
    ]


def aggregate_case_metrics(cases: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    rows = list(cases)
    if not rows:
        raise ValueError("ablation_cases_required")
    return {
        metric: sum(float(row.get(metric) or 0.0) for row in rows) / len(rows)
        for metric in QUALITY_METRICS
    }


def first_systematic_regression(
    results: Mapping[str, Mapping[str, float]],
    *,
    tolerance: float = 1e-9,
) -> dict[str, Any]:
    plan = progressive_ablation_plan()
    previous: Mapping[str, float] | None = None
    evaluated: list[str] = []
    for stage in plan:
        identifier = stage["id"]
        if identifier not in results:
            return {
                "stopped": True,
                "reason": "next_ablation_not_evaluated",
                "next": identifier,
                "evaluated": evaluated,
            }
        current = results[identifier]
        evaluated.append(identifier)
        if previous is not None:
            regressions = [
                metric
                for metric in QUALITY_METRICS
                if float(current.get(metric) or 0.0) + tolerance < float(previous.get(metric) or 0.0)
            ]
            protected = [metric for metric in regressions if metric in {"schema_validity", "rule_accuracy", "source_accuracy"}]
            semantic = [metric for metric in regressions if metric in {"contradiction_recall", "counterargument_coverage"}]
            if protected or len(semantic) == 2:
                return {
                    "stopped": True,
                    "reason": "systematic_regression",
                    "stage": identifier,
                    "responsible_arc": stage["introduced_arc"],
                    "regressed_metrics": regressions,
                    "evaluated": evaluated,
                }
        previous = current
    return {"stopped": False, "reason": "all_ablations_completed", "evaluated": evaluated}


def ablation_preflight(*, direct_gate: Mapping[str, Any], pipeline_micro_gate: Mapping[str, Any]) -> dict[str, Any]:
    if not direct_gate.get("passed"):
        raise RuntimeError("instruct_base_insufficient")
    if not pipeline_micro_gate.get("passed"):
        raise RuntimeError("instruct_pipeline_micro_overfit_required")
    return {
        "ok": True,
        "cases": 8,
        "plan": progressive_ablation_plan(),
        "test_split_allowed": True,
        "external_actions": False,
    }


def test_split_allowed(pipeline_micro_gate: Mapping[str, Any]) -> bool:
    return bool(pipeline_micro_gate.get("passed"))


def adoption_gate(
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
    *,
    relative_threshold: float = 0.10,
) -> dict[str, Any]:
    improved: list[str] = []
    for metric in ADOPTION_IMPROVEMENT_METRICS:
        old = float(baseline.get(metric) or 0.0)
        new = float(candidate.get(metric) or 0.0)
        if (old == 0.0 and new > 0.0) or (old > 0.0 and (new - old) / old >= relative_threshold):
            improved.append(metric)
    regressions = [
        metric
        for metric in NON_REGRESSION_METRICS
        if float(candidate.get(metric) or 0.0) < float(baseline.get(metric) or 0.0)
    ]
    baseline_hallucination = float(baseline.get("hallucination_rate") or 0.0)
    candidate_hallucination = float(candidate.get("hallucination_rate") or 0.0)
    if candidate_hallucination > baseline_hallucination:
        regressions.append("hallucination_rate")
    passed = bool(improved) and not regressions
    return {
        "passed": passed,
        "relative_threshold": relative_threshold,
        "improved_metrics": improved,
        "regressions": regressions,
        "production_enable_allowed": False,
    }
