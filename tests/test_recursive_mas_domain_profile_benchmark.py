from ralfloop_agent.domains.recursive_domain_reasoning_benchmark import ReasoningCase
from ralfloop_agent.domains.recursive_mas_domain_dataset import deterministic_splits, generate_dataset
from ralfloop_agent.domains.recursive_mas_domain_profile_benchmark import (
    DomainProfileBenchmark,
    RUNNERS,
    adoption_decision,
)


def _aggregates(value, *, schema=1.0):
    return {
        "evaluable_count": 36,
        "schema_validity": schema,
        "rule_accuracy": 1.0,
        "source_accuracy": 1.0,
        "contradiction_recall": value,
        "counterargument_coverage": value,
        "uncertainty_calibration": value,
        "recommendation_usefulness": value,
        "hallucination_rate": 0.0,
        "safety_violations": 0,
        "approval_violations": 0,
    }


def test_benchmark_uses_only_unseen_test_split():
    cases, _ = generate_dataset()
    splits = deterministic_splits(cases)
    test_ids = set(splits["test"])
    selected = [ReasoningCase.from_dict(case) for case in cases if case["id"] in test_ids]
    assert len(selected) == 36
    assert {case.id for case in selected}.isdisjoint(splits["train"])
    assert {case.id for case in selected}.isdisjoint(splits["validation"])


def test_adoption_requires_ten_percent_gain_without_regression():
    decision = adoption_decision(
        {
            "single_qwen_7b_with_domain": _aggregates(0.5),
            "recursive_mas_domain_reasoning_checkpoint": _aggregates(0.56),
        }
    )
    assert decision.adopted is True
    assert decision.threshold_met


def test_adoption_rejects_provenance_or_schema_regression():
    candidate = _aggregates(0.8, schema=0.9)
    candidate["source_accuracy"] = 0.9
    decision = adoption_decision(
        {
            "single_qwen_7b_with_domain": _aggregates(0.5),
            "recursive_mas_domain_reasoning_checkpoint": candidate,
        }
    )
    assert decision.adopted is False
    assert "schema_validity" in decision.regressions
    assert "source_accuracy" in decision.regressions


def test_missing_runner_is_infrastructure_error_not_failure(tmp_path):
    cases, _ = generate_dataset()
    item = ReasoningCase.from_dict(cases[0])
    manifest = DomainProfileBenchmark(tmp_path).run([item], {})
    for name in RUNNERS:
        aggregate = manifest["aggregates"][name]
        assert aggregate["evaluable_count"] == 0
        assert aggregate["infrastructure_errors"] == 1
    assert manifest["decision"]["adopted"] is False
    assert manifest["codex_is_judge"] is False
