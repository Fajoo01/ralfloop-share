from __future__ import annotations

from fastapi.testclient import TestClient

from ralfloop_agent.integration.capability_adapter import route_to_legacy_dict
from src.api import app
from src.router import route_task
from src.skills import SkillsRegistry


def test_skill_registry_loads_enabled_external_manifests():
    skills = SkillsRegistry()

    assert "bottazzi_citofono" in skills.skills
    assert "atm_telegram" in skills.skills
    assert "bandi_rag" not in skills.skills


def test_skill_registry_avoids_generic_telegram_false_positive():
    skills = SkillsRegistry()

    matched = skills.match("invia telegram con risultato finale")

    assert "atm_telegram" not in matched


def test_skill_registry_routes_bottazzi_gate_to_citofono_skill():
    skills = SkillsRegistry()

    matched = skills.match("bot-tazzi apri cancello da telegram")

    assert "bottazzi_citofono" in matched
    assert "atm_telegram" not in matched


def test_telegram_rl_routes_relcalc_not_atm():
    skills = SkillsRegistry()

    matched = skills.match("telegram rl usa abc_relcalc calcolatrice relazionale")

    assert "abc_relcalc" in matched
    assert "atm_telegram" not in matched


def test_explicit_telepathy_requires_jury():
    route = route_task("telepatia: fai giuria multiagent sul piano")

    assert route.jury.enabled is True
    assert route.jury.mode == "required"
    assert "giudice" in route.jury.roles
    assert route.skills_used == []
    assert route.jury_policy.requires_final_review is True
    assert route.collaboration_backend.backend_name == "text_mas_proxy"
    assert route.collaboration_backend.implementation_level == "text_proxy"
    assert route.collaboration_backend.style == "sequential"
    assert route.collaboration_backend.native_latent is False
    assert route.collaboration_backend.style_selection_source == "ralfloop_local_policy"
    assert route.verification_policy.verifier_type == "deterministic"


def test_external_action_requires_jury_and_confirmation():
    route = route_task("invia telegram con risultato finale")

    assert route.mode == "external_action"
    assert route.requires_confirmation is True
    assert route.jury.mode == "required"
    assert route.jury_policy.requires_human_confirmation is True
    assert route.collaboration_backend.backend_name == "text_mas_proxy"
    assert route.collaboration_backend.style == "deliberation"


def test_patch_task_gets_advisory_jury():
    route = route_task("fix bug concreto con test")

    assert route.mode == "patch_allowed"
    assert route.jury.mode == "advisory"


def test_legacy_route_exposes_jury_contract():
    route = route_task("invia email finale")
    payload = route_to_legacy_dict(route)

    assert payload["needs_jury"] is True
    assert payload["jury_mode"] == "required"
    assert payload["collaboration_backend"]["backend_name"] == "text_mas_proxy"
    assert payload["collaboration_backend"]["implementation_level"] == "text_proxy"
    assert payload["verification_policy"]["verifier_type"] == "combined"
    assert "final_answer_without_jury_review" in payload["blocked_actions"]
    assert payload["workflow"][0] == "text_mas_deliberation"


def test_api_route_only_returns_text_mas_trace():
    response = TestClient(app).post(
        "/tasks/run",
        json={"user_goal": "telepatia giuria multiagent", "mode": "route_only"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["jury_policy"]["mode"] == "required"
    assert payload["collaboration_backend"]["implementation_level"] == "text_proxy"
    assert payload["collaboration_trace"]["backend_name"] == "text_mas_proxy"
    assert payload["collaboration_trace"]["trace_is_native_recursive_mas"] is False
    assert payload["collaboration_trace"]["loop"][-1]["phase"] == "final_review"
    assert payload["jury_trace"] == payload["collaboration_trace"]
