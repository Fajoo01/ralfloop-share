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
            return {"ok": True, "answer": "Mantieni una strategia prudente.", "selected_backend": "recursive_mas_native"}

    result = answer_goal("servizio down molti utenti, strategia prudente", "incident_triage", jury_controller=Controller())
    assert result["resolution_type"] == "jury"
    assert result["external_action_executed"] is False
    assert result["recommendations"] == ["Mantieni una strategia prudente."]
