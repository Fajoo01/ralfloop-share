from fastapi.testclient import TestClient

from src.api import app
from src.router import route_task


client = TestClient(app)


def test_check_only_mode():
    response = client.post("/tasks/run", json={"user_goal": "controlla log jellyfin"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["route"]["mode"] == "check_only"
    assert payload["route"]["skills_used"] == ["jellyfin"]
    assert payload["route"]["requires_confirmation"] is False
    assert payload["evidence"]["command"] == "ls -la"
    assert payload["evidence"]["path"]
    assert payload["evidence"]["exit_code"] == 0


def test_patch_allowed_mode():
    response = client.post("/tasks/run", json={"user_goal": "fix bug concreto con test"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["route"]["mode"] == "patch_allowed"
    assert "diff" in payload["evidence"]
    assert payload["evidence"]["tests"]
    assert payload["evidence"]["command"] == "pwd"
    assert payload["evidence"]["exit_code"] == 0


def test_external_action_confirmation():
    response = client.post("/tasks/run", json={"user_goal": "invia telegram con risultato finale"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["route"]["mode"] == "external_action"
    assert payload["route"]["requires_confirmation"] is True
    assert payload["pending_confirmation_id"]
    assert payload["evidence"]["command"] == "mcp:external_action"
    assert payload["evidence"]["path"] == "external"
    assert payload["evidence"]["exit_code"] == 0

    approval = client.post(f"/confirmations/{payload['pending_confirmation_id']}/approve")
    assert approval.status_code == 200
    assert approval.json()["executed"] is True


def test_route_only_dry_run():
    response = client.post(
        "/tasks/run",
        json={"user_goal": "controlla log garden detector", "mode": "route_only"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["route"]["mode"] == "check_only"
    assert payload["route"]["skills_used"] == ["garden_detector"]
    assert payload["evidence"] is None
    assert payload["pending_confirmation_id"] is None


def test_router_falls_back_to_check_only():
    route = route_task("dimmi lo stato generale")

    assert route.mode == "check_only"
    assert "defaulted to check_only" in route.reasoning
