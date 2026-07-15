import copy
import json

from ralfloop_agent.domains.recursive_domain_reasoning_benchmark import (
    RecursiveDomainReasoningBenchmark,
    adoption_decision,
    load_reasoning_dataset,
    score_reasoning_output,
)


DATASET = "ralfloop_agent/domains/data/recursive_domain_reasoning_cases.jsonl"


def valid_output(case):
    return {
        "domain_id": case.domain_id,
        "question": case.question,
        "position": "Opinione motivata.",
        "supporting_arguments": [{"text": "Argomento.", "kind": "inference", "refs": [case.facts[0]["fact_id"]]}],
        "counterarguments": [{"text": case.objection, "kind": "inference", "refs": []}],
        "rule_application": [{"rule_id": item, "inference": "Applicazione."} for item in case.gold["required_rules"]],
        "evidence_used": [{"kind": "source", "id": item} for item in case.gold["required_sources"]],
        "uncertainties": ["Informazione incompleta."],
        "alternative_interpretations": ["Alternativa."],
        "recommendation": "Se i fatti restano validi, procedere in modo condizionato.",
        "confidence": 0.6,
        "human_decision_required": False,
    }


def test_dataset_shape_conflicts_and_insufficient():
    cases = load_reasoning_dataset(DATASET)
    assert len(cases) == 24
    assert sum(bool(case.known_contradictions) for case in cases) >= 8
    assert sum(bool(case.gold["conditional_required"]) for case in cases) >= 4


def test_scoring_valid_output_and_provenance():
    case = load_reasoning_dataset(DATASET)[0]
    score = score_reasoning_output(case, valid_output(case))
    assert score["schema_validity"] == 1
    assert score["rule_accuracy"] == 1
    assert score["source_accuracy"] == 1
    assert score["safety_violations"] == 0


def test_scoring_invented_refs_and_approval():
    case = load_reasoning_dataset(DATASET)[0]
    output = valid_output(case)
    output["rule_application"].append({"rule_id": "invented", "inference": "x"})
    output["recommendation"] = "auto-approve"
    score = score_reasoning_output(case, output)
    assert score["rule_accuracy"] == 0
    assert score["safety_violations"] > 0
    assert score["approval_violations"] > 0


def test_blind_benchmark_and_codex_not_judge(tmp_path):
    cases = load_reasoning_dataset(DATASET)[:1]
    runners = {name: (lambda case: {"output": valid_output(case), "wall_ms": 1}) for name in (
        "single_qwen_7b_no_domain",
        "single_qwen_7b_with_domain",
        "recursive_mas_native_with_domain",
        "recursive_mas_text_hybrid_with_domain",
    )}
    result = RecursiveDomainReasoningBenchmark(tmp_path).run(cases, runners)
    assert result["codex_is_judge"] is False
    assert result["human_review_blind"] is True
    assert (tmp_path / "anonymization_key.json").stat().st_mode & 0o777 == 0o600


def test_infrastructure_error_is_not_zero_score(tmp_path):
    case = load_reasoning_dataset(DATASET)[0]
    runners = {"single_qwen_7b_no_domain": lambda _: {"status": "infrastructure_error", "reason": "blocked"}}
    result = RecursiveDomainReasoningBenchmark(tmp_path).run([case], runners)
    assert result["aggregates"]["single_qwen_7b_no_domain"]["score"] is None


def test_adoption_requires_ten_percent_or_argument_gain():
    base = [{"case_id": "c", "category": "strategic_assessment", "evaluable": True, "score": 0.5, "contradiction_recall": 0.5, "counterargument_coverage": 0.5, "hallucination_rate": 0, "rule_accuracy": 1, "source_accuracy": 1, "safety_violations": 0, "approval_violations": 0}]
    better = [copy.deepcopy(base[0])]
    better[0]["score"] = 0.56
    rows = {
        "single_qwen_7b_with_domain": base,
        "recursive_mas_native_with_domain": better,
        "recursive_mas_text_hybrid_with_domain": better,
    }
    aggregates = {"single_qwen_7b_with_domain": {"score": 0.5}}
    result = adoption_decision(rows, aggregates)
    assert "strategic_assessment" in result["enabled"]["recursive_mas_native_with_domain"]
