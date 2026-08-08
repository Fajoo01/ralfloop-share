import json

from fastapi.testclient import TestClient

from openshell_backend.app import app
from ralfloop_agent.core.capability_router import route_task


def test_check_only_garden_routes_to_evidence_no_write():
    route = route_task(
        "Controlla falso positivo detector giardino. Non modificare file. "
        "Raccogli evidence da log e codice."
    )

    assert route.task_mode == "check_only"
    assert route.write_policy == "no_write"
    assert route.evidence_first is True
    assert "garden_detector" in route.domain_skills
    assert "command" in route.required_output_fields
    assert "path" in route.required_output_fields
    assert "exit_code" in route.required_output_fields


def test_patch_task_routes_to_repro_patch_tests():
    route = route_task(
        "PATCH TASK. Correggi il bug in tmp/probe.py, poi esegui py_compile e pytest."
    )

    assert route.task_mode == "patch_allowed"
    assert route.write_policy == "sandbox_write_allowed_after_repro"
    assert "reproduce_failure" in route.workflow
    assert "apply_minimal_patch" in route.workflow
    assert "run_targeted_tests" in route.workflow
    assert "patch_without_repro" in route.blocked_actions


def test_email_send_routes_to_mcp_with_human_confirmation():
    route = route_task(
        "Usa bandi e manda email alla Regione Lombardia dopo conferma umana."
    )

    assert route.task_mode == "external_action"
    assert route.write_policy == "external_side_effect_requires_confirmation"
    assert route.needs_human_confirmation is True
    assert "bandi" in route.domain_skills
    assert "google_workspace.gmail" in route.mcp_connectors
    assert "send_without_human_confirmation" in route.blocked_actions


def test_reading_posta_routes_to_protected_gmail_not_substring() -> None:
    posta = route_task("Leggi la posta")
    sposta = route_task("Non spostare nulla")
    assert posta.task_mode == "external_action"
    assert posta.needs_human_confirmation is True
    assert "google_workspace.gmail" in posta.mcp_connectors
    assert "google_workspace.gmail" not in sposta.mcp_connectors


def test_human_confirmed_clears_confirmation_gate_but_keeps_external_policy():
    route = route_task(
        "invia email alla Regione Lombardia",
        extra_context={"human_confirmed": True},
    )

    assert route.task_mode == "external_action"
    assert route.needs_human_confirmation is False
    assert route.write_policy == "external_side_effect_requires_confirmation"


def test_route_only_endpoint_returns_capability_route_without_agent_loop():
    client = TestClient(app)
    response = client.post(
        "/tasks/run",
        json={
            "user_goal": "Controlla email_ops. Non modificare file.",
            "mode": "route_only",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["current_role"] == "capability_router"
    assert payload["stop_reason"] == "route_only"
    assert payload["capability_route"]["task_mode"] == "check_only"
    assert json.loads(payload["final_answer"])["write_policy"] == "no_write"

def test_abc_relcalc_routes_to_calculator_not_memory():
    route = route_task("usa abc_relcalc calcolatrice relazionale con pytest")

    assert "abc_relcalc" in route.domain_skills
    assert "abc_memory" not in route.domain_skills


def test_rl_abc_routes_to_memory_not_calculator():
    route = route_task("rl:abc aggiorna memoria")

    assert "abc_memory" in route.domain_skills
    assert "abc_relcalc" not in route.domain_skills
