from fastapi.testclient import TestClient

from openshell_backend.app import app
from ralfloop_agent.integration.capability_adapter import route_task, route_to_legacy_dict
from ralfloop_agent.models.result_envelope import ResultEnvelope
from src.models import Evidence


client = TestClient(app)


def test_adapter_routes_with_reasoning_cycle_metadata():
    route = route_task("controlla log jellyfin")

    assert route.mode == "check_only"
    assert "jellyfin" in route.skills_used
    assert "reasoning_cycle_status=" in route.reasoning


def test_legacy_route_preserves_existing_keys():
    route = route_task("fix bug concreto")
    payload = route_to_legacy_dict(route)

    assert payload["task_mode"] == "patch_allowed"
    assert payload["write_policy"] == "sandbox_write_allowed_after_repro"
    assert payload["evidence_first"] is True
    assert "command" in payload["required_output_fields"]
    assert "patch_without_repro" in payload["blocked_actions"]


def test_result_envelope_is_json_safe():
    route = route_task("controlla log")
    envelope = ResultEnvelope(
        route=route,
        evidence=Evidence(command="ls -la", path="/tmp", exit_code=0),
        answer="ok",
        meta={"source": "test"},
    )

    payload = envelope.model_dump(mode="json")

    assert payload["route"]["mode"] == "check_only"
    assert payload["evidence"]["command"] == "ls -la"
    assert payload["answer"] == "ok"


def test_openshell_route_only_uses_integration_adapter():
    response = client.post(
        "/tasks/run",
        json={"user_goal": "controlla log garden detector", "mode": "route_only"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["stop_reason"] == "route_only"
    assert payload["capability_route"]["task_mode"] == "check_only"
    assert "garden_detector" in payload["capability_route"]["domain_skills"]
    assert payload["result_envelope"]["route"]["mode"] == "check_only"


def test_openshell_external_action_requires_confirmation_before_runtime():
    response = client.post(
        "/tasks/run",
        json={"user_goal": "usa bandi e invia email alla Regione Lombardia"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is False
    assert payload["stop_reason"] == "human_confirmation_required"
    assert payload["pending_confirmation_id"]
    assert payload["capability_route"]["task_mode"] == "external_action"
    assert "bandi" in payload["capability_route"]["domain_skills"]
    assert "google_workspace.gmail" in payload["capability_route"]["mcp_connectors"]
    assert payload["result_envelope"]["evidence"]["command"] == "mcp:external_action"

    approval = client.post(f"/confirmations/{payload['pending_confirmation_id']}/approve")
    assert approval.status_code == 200
    assert approval.json()["executed"] is True
