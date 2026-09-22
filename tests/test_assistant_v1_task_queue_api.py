from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from openshell_backend import assistant_v1_api
from ralfloop_agent.unified_assistant.task_queue import BotTazziTaskQueue


def client(tmp_path: Path) -> TestClient:
    app = FastAPI()
    app.include_router(assistant_v1_api.router)
    queue = BotTazziTaskQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)
    app.dependency_overrides[assistant_v1_api.get_task_queue] = lambda: queue
    return TestClient(app)


def test_task_api_create_list_pin_and_next(tmp_path: Path) -> None:
    api = client(tmp_path)

    general = api.post(
        "/assistant/v1/tasks",
        json={"title": "Aggiorna documentazione"},
    ).json()["task"]
    money = api.post(
        "/assistant/v1/tasks",
        json={"title": "Controlla pagamento TARI"},
    ).json()["task"]

    snapshot = api.get("/assistant/v1/tasks").json()
    assert snapshot["classifier"] == "JED"
    assert snapshot["tasks"][0]["task"]["task_id"] == money["task_id"]

    pinned = api.post(
        f"/assistant/v1/tasks/{general['task_id']}/pin",
        json={"rank": 1},
    )
    assert pinned.status_code == 200
    assert pinned.json()["tasks"][0]["task"]["task_id"] == general["task_id"]

    next_payload = api.get("/assistant/v1/tasks/next").json()
    assert next_payload["task"]["task_id"] == general["task_id"]


def test_task_api_blocked_requires_reason(tmp_path: Path) -> None:
    api = client(tmp_path)
    task = api.post(
        "/assistant/v1/tasks",
        json={"title": "Controlla bonifico"},
    ).json()["task"]

    response = api.post(
        f"/assistant/v1/tasks/{task['task_id']}/state",
        json={"state": "blocked"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "blocked_reason_required"


def test_bottazzi_ui_exposes_human_reorderable_queue(tmp_path: Path) -> None:
    api = client(tmp_path)

    response = api.get("/assistant/v1")

    assert response.status_code == 200
    assert "Coda dei compiti" in response.text
    assert "priorità alta: soldi, amore, famiglia" in response.text
    assert "/assistant/v1/tasks" in response.text
    assert "Fissa" in response.text
    assert "Libera" in response.text
