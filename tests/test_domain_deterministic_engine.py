from ralfloop_agent.domains.cli import answer_goal
from ralfloop_agent.domains.deterministic_engine import DeterministicEngine
from ralfloop_agent.domains.registry import fixture_domain


def test_deterministic_answer_without_jury():
    result = answer_goal("Quanto fa 2 + 3?", "arithmetic_basic")
    assert result["resolution_type"] == "deterministic"
    assert result["answer"] == "5"
    assert result["jury_invoked"] is False


def test_formula_complete_without_jury():
    domain = fixture_domain("arithmetic_basic")
    out = DeterministicEngine().evaluate(domain, "2 + 3")
    assert out["complete"] is True
    assert out["result"] == "5"


def test_mixed_executes_deterministic_before_jury():
    domain = fixture_domain("incident_triage")
    out = DeterministicEngine().evaluate(domain, "servizio down molti utenti, strategia prudente")
    assert "sev1_down" in out["matched_rules"]
    assert "recommendation_required" in out["unresolved_questions"]
    assert out["complete"] is False


def test_side_effect_requires_human_confirmation():
    result = answer_goal("Invia telegram per incidente", "incident_triage")
    assert result["status"] == "human_confirmation_required"
    assert result["external_action_executed"] is False


def test_jury_result_is_structured_with_mock_controller():
    class Config:
        enabled = True

    class Controller:
        config = Config()

        def execute(self, request):
            assert "recommendation_required" in request["goal"]
            return {
                "ok": True,
                "answer": {
                    "domain_id": "incident_triage",
                    "question": "servizio down molti utenti, strategia prudente",
                    "position": "È prudente contenere prima di ottimizzare.",
                    "supporting_arguments": [{"text": "La gravità suggerisce cautela.", "kind": "inference", "refs": ["local_incident_policy"]}],
                    "counterarguments": [{"text": "La rapidità può ridurre il danno.", "kind": "inference", "refs": []}],
                    "rule_application": [{"rule_id": "sev1_down", "inference": "Applicabile al fatto osservato."}],
                    "evidence_used": [{"kind": "source", "id": "local_incident_policy"}],
                    "uncertainties": ["Bilanciamento rapidità-sicurezza non risolto."],
                    "alternative_interpretations": ["Intervento rapido ma reversibile."],
                    "recommendation": "Contenere con passaggi reversibili e revisione umana.",
                    "confidence": 0.7,
                    "human_decision_required": True,
                },
                "selected_backend": "recursive_mas_native",
            }

    result = answer_goal(
        "servizio down molti utenti, strategia prudente",
        "incident_triage",
        jury_controller=Controller(),
        explicit_recursive=True,
    )
    assert result["resolution_type"] == "jury"
    assert result["external_action_executed"] is False
    assert result["recommendations"] == ["Contenere con passaggi reversibili e revisione umana."]


def test_recursive_default_off_at_orchestrator_level():
    class NeverCalled:
        @property
        def config(self):
            raise AssertionError("recursive controller must not be inspected")

    result = answer_goal(
        "servizio down molti utenti, strategia prudente",
        "incident_triage",
        jury_controller=NeverCalled(),
    )
    assert result["status"] == "domain_reasoning_required"
    assert result["routing"]["selected_backend"] == "single_qwen_7b_with_domain"
