import copy

from ralfloop_agent.domains.domain_opinion import (
    DomainReasoningInput,
    build_domain_reasoning_prompt,
    execute_domain_opinion,
    validate_domain_opinion,
)
from ralfloop_agent.domains.registry import fixture_domain


def request():
    domain = fixture_domain("incident_triage")
    return DomainReasoningInput.from_domain(
        domain,
        "Quale strategia prudente?",
        facts=[{"fact_id": "f1", "statement": "servizio down", "kind": "fact"}],
        reason_codes=["conflicting_sources", "recommendation_required"],
    )


def opinion():
    return {
        "domain_id": "incident_triage",
        "question": "Quale strategia prudente?",
        "position": "La cautela è preferibile.",
        "supporting_arguments": [{"text": "Riduce il rischio.", "kind": "inference", "refs": ["f1"]}],
        "counterarguments": [{"text": "Può rallentare il ripristino.", "kind": "inference", "refs": []}],
        "rule_application": [{"rule_id": "sev1_down", "inference": "La regola orienta la priorità."}],
        "evidence_used": [{"kind": "fact", "id": "f1"}, {"kind": "source", "id": "local_incident_policy"}],
        "uncertainties": ["Il compromesso rapidità-sicurezza resta aperto."],
        "alternative_interpretations": ["Ripristino rapido e reversibile."],
        "recommendation": "Procedere per passi reversibili.",
        "confidence": 0.7,
        "human_decision_required": True,
    }


def test_domain_required_except_creation_review():
    try:
        DomainReasoningInput("", "", "q", (), (), (), (), (), {}, ())
    except ValueError as exc:
        assert str(exc) == "domain_required"
    else:
        raise AssertionError("missing domain accepted")
    DomainReasoningInput("", "", "q", (), (), (), (), (), {}, ("domain_creation_review",))


def test_planner_alternatives_critic_counterargument_solver_uncertainty():
    prompt = build_domain_reasoning_prompt(request())
    assert "alternative hypotheses" in prompt
    assert "strongest counterargument" in prompt
    assert "unresolved critic objections as uncertainties" in prompt
    assert "chain of thought" in prompt


def test_rule_and_source_provenance_valid():
    result = validate_domain_opinion(opinion(), request())
    assert result["ok"] is True
    assert result["rule_ids"] == ["sev1_down"]
    assert result["source_ids"] == ["local_incident_policy"]


def test_invented_source_rejected():
    value = copy.deepcopy(opinion())
    value["evidence_used"].append({"kind": "source", "id": "invented"})
    assert "invented_source" in validate_domain_opinion(value, request())["errors"]


def test_invented_rule_rejected():
    value = copy.deepcopy(opinion())
    value["rule_application"].append({"rule_id": "invented", "inference": "x"})
    assert "invented_rule" in validate_domain_opinion(value, request())["errors"]


def test_solver_must_preserve_critic_uncertainty():
    value = copy.deepcopy(opinion())
    value["uncertainties"] = []
    assert "solver_ignored_unresolved_critic" in validate_domain_opinion(value, request())["errors"]


def test_invalid_output_rejected_without_action():
    result = execute_domain_opinion(request(), lambda _: {"ok": True, "answer": "not json"})
    assert result["status"] == "recursive_output_invalid"
    assert result["external_action_executed"] is False


def test_approval_invariants():
    value = copy.deepcopy(opinion())
    value["recommendation"] = "auto-approve"
    result = validate_domain_opinion(value, request())
    assert "forbidden_action_or_approval" in result["errors"]
