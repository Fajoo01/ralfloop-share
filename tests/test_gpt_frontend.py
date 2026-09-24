from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import threading
from urllib.parse import urlparse

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget
from ralfloop_agent.integration.gpt_frontend import GptWorkController, create_server
from ralfloop_agent.integration.gpt_work_queue import GptJobState, GptWorkQueue


class FakeCdp:
    def __init__(self) -> None:
        self.endpoint = "http://127.0.0.1:9238"
        self._targets: list[BrowserTarget] = []
        self.started: list[dict] = []
        self.installed: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.messages: list[tuple[str, str, str]] = []
        self.activated: list[str] = []

    def targets(self):
        return list(self._targets)

    def chatgpt_companion_state(self, target_id: str):
        return {"ghost": False, "focused": False, "busy": False, "composer_chars": 0}

    def install_human_input_target(self, target_id: str, conversation_url: str):
        self.installed.append((target_id, conversation_url))
        return {"ok": True, "conversation_url": conversation_url}

    def close_target(self, target_id: str):
        self.closed.append(target_id)
        self._targets = [target for target in self._targets if target.target_id != target_id]

    def activate_target(self, target_id: str):
        if not any(target.target_id == target_id for target in self._targets):
            raise RuntimeError("target_missing")
        self.activated.append(target_id)

    def queue_human_message(self, target_id: str, conversation_url: str, text: str):
        self.messages.append((target_id, conversation_url, text))
        return {"ok": True, "queued": True}

    def create_chatgpt_target(self, *, clear_cache: bool = False, background: bool = True):
        target_id = f"reopen-{len(self._targets)+1}"
        self._targets.append(BrowserTarget(target_id, "page", "https://chatgpt.com/", "ChatGPT", f"ws://{target_id}"))
        return target_id

    def navigate_chatgpt_conversation(self, target_id: str, url: str):
        for index, target in enumerate(self._targets):
            if target.target_id == target_id:
                self._targets[index] = BrowserTarget(target_id, "page", url, target.title, target.websocket_url)
                return {"authenticated": True, "ready": True}
        raise RuntimeError("target_missing")

    def start_chatgpt_job(self, prompt: str, *, new_chat_url: str, background: bool, submit: bool, wait_timeout_s: float = 30.0):
        number = len(self.started) + 1
        target_id = f"target-{number}"
        canonical = f"https://chatgpt.com/c/chat-{number}"
        parsed = urlparse(new_chat_url)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] == "g":
            context = f"https://chatgpt.com/g/{parts[1]}/c/chat-{number}"
        else:
            context = canonical
        target = BrowserTarget(target_id, "page", context, f"Job {number}", f"ws://{target_id}")
        self._targets.append(target)
        record = {
            "prompt": prompt,
            "new_chat_url": new_chat_url,
            "background": background,
            "submit": submit,
            "target_id": target_id,
            "conversation_url": canonical,
            "conversation_context_url": context,
        }
        self.started.append(record)
        return {
            "new_target_id": target_id,
            "conversation_url": canonical,
            "conversation_context_url": context,
            "server_chat_deleted": False,
        }


def make_queue(tmp_path: Path) -> GptWorkQueue:
    return GptWorkQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def running_frontend(queue: GptWorkQueue, cdp: FakeCdp):
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    server = create_server(queue, cdp, origin=origin)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, origin
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(port: int, method: str, path: str, *, origin: str, payload=None, extra_headers=None):
    headers = dict(extra_headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers.setdefault("Content-Type", "application/json")
        headers.setdefault("X-Bottazzi-Frontend", "1")
        headers.setdefault("Origin", origin)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    decoded = json.loads(data.decode("utf-8")) if response.getheader("Content-Type", "").startswith("application/json") else data.decode("utf-8")
    return response.status, decoded


def test_pump_respects_limit_and_preserves_project_context(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    queue.set_max_open_chats(2)
    for number in range(3):
        queue.create_job(
            f"Job {number}",
            prompt=f"Do {number}",
            project_name="Colletti",
            project_url="https://chatgpt.com/g/g-p-colletti/project",
        )
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)

    result = controller.pump()

    assert len(result["started"]) == 2
    jobs = queue.list_jobs()
    assert [job.state for job in jobs] == [GptJobState.ACTIVE, GptJobState.ACTIVE, GptJobState.QUEUED]
    assert jobs[0].conversation_url == "https://chatgpt.com/c/chat-1"
    assert jobs[0].conversation_context_url == "https://chatgpt.com/g/g-p-colletti/c/chat-1"
    assert cdp.started[0]["new_chat_url"] == "https://chatgpt.com/g/g-p-colletti/project"
    assert result["slots"] == {"used": 2, "limit": 2, "free": 0}


def test_reconcile_rebinds_changed_target_id_by_conversation(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job("One", prompt="Do one")
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    controller.pump(max_to_start=1)
    active = queue.get_job(job.job_id)
    assert active.target_id == "target-1"

    cdp._targets = [BrowserTarget("replacement", "page", "https://chatgpt.com/c/chat-1", "One", "ws://replacement")]
    controller.reconcile()

    rebound = queue.get_job(job.job_id)
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.target_id == "replacement"
    assert rebound.conversation_context_url == "https://chatgpt.com/c/chat-1"


def test_missing_active_target_moves_to_review_without_deleting_job(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job("One", prompt="Do one")
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    controller.pump(max_to_start=1)
    cdp._targets = []

    controller.reconcile()

    reviewed = queue.get_job(job.job_id)
    assert reviewed.state is GptJobState.REVIEW
    assert reviewed.conversation_url == "https://chatgpt.com/c/chat-1"
    assert reviewed.last_error == "chat_not_open_locally"


def test_frontend_api_groups_projects_and_auto_starts(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    queue.set_max_open_chats(1)
    cdp = FakeCdp()
    with running_frontend(queue, cdp) as (port, origin):
        status, created = request(
            port,
            "POST",
            "/api/jobs",
            origin=origin,
            payload={
                "title": "Filologia",
                "prompt": "Riprendi Scholarly",
                "project_name": "Indipendentemenza dai colletti bianchi",
                "project_url": "https://chatgpt.com/g/g-p-colletti/project",
                "auto_start": True,
            },
        )
        assert status == 200
        assert len(created["pump"]["started"]) == 1

        status, state = request(port, "GET", "/api/state", origin=origin)
        assert status == 200
        project = state["queue"]["projects"][0]
        assert project["project_name"] == "Indipendentemenza dai colletti bianchi"
        assert project["project_url"] == "https://chatgpt.com/g/g-p-colletti/project"
        assert project["jobs"][0]["conversation_context_url"] == "https://chatgpt.com/g/g-p-colletti/c/chat-1"
        assert state["runtime"]["slots"]["used"] == 1


def test_frontend_rejects_cross_origin_mutation(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    with running_frontend(queue, cdp) as (port, origin):
        status, body = request(
            port,
            "POST",
            "/api/settings",
            origin=origin,
            payload={"max_open_chats": 4},
            extra_headers={"Origin": "https://example.com"},
        )

    assert status == 403
    assert body["error"] == "origin_not_allowed"
