from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import socket
import threading
from urllib.parse import urlparse

import pytest

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, CdpError
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
        self.history_records: list[dict[str, str]] = []
        self.history_projects: list[dict[str, str]] = []
        self.resolved_project_urls: dict[str, str] = {}

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

    def create_target(self, url: str, *, background: bool = False):
        target_id = f"temp-{len(self._targets)+1}"
        self._targets.append(BrowserTarget(target_id, "page", url, "ChatGPT", f"ws://{target_id}"))
        return target_id

    def project_records(self, target_id: str, *, wait_timeout_s: float = 12.0):
        return list(self.history_projects)

    def resolve_project_url(self, target_id: str, project_name: str, *, wait_timeout_s: float = 12.0):
        if project_name not in self.resolved_project_urls:
            raise RuntimeError("project_not_found")
        return self.resolved_project_urls[project_name]

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

    def conversation_records(self, target_id: str, *, reload: bool = False, wait_timeout_s: float = 3.0):
        return list(self.history_records)

    def account_catalog(self, target_id: str, **kwargs):
        projects = self.history_projects or [{"title": name, "url": url, "project_id": url.split("/")[4]} for name, url in self.resolved_project_urls.items()]
        return {"chats": list(self.history_records), "projects": list(projects), "complete": True, "projects_complete": True, "source": "chatgpt_query_client"}

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


def test_dashboard_distinguishes_real_progress_from_stale_busy_flag(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Stalled",
        conversation_url="https://chatgpt.com/c/job",
        conversation_context_url="https://chatgpt.com/c/job",
        target_id="managed",
        state=GptJobState.ACTIVE,
    )

    class ActivityCdp(FakeCdp):
        def __init__(self) -> None:
            super().__init__()
            self._targets = [BrowserTarget("managed", "page", "https://chatgpt.com/c/job", "Stalled", "ws://managed")]

        def chatgpt_companion_state(self, target_id: str):
            return {
                "ghost": False,
                "focused": False,
                "busy": True,
                "composer_chars": 0,
                "user_turns": 2,
                "assistant_turns": 1,
                "tool_activity_count": 0,
                "last_assistant_text": "Risposta precedente",
            }

    controller = GptWorkController(queue, ActivityCdp())
    snapshot = controller.dashboard_snapshot()
    current = next(item for item in snapshot["jobs"] if item["job_id"] == job.job_id)
    assert current["live_busy"] is False
    assert current["live_pending"] is True


def test_dashboard_marks_current_assistant_turn_as_real_work(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Working",
        conversation_url="https://chatgpt.com/c/job",
        conversation_context_url="https://chatgpt.com/c/job",
        target_id="managed",
        state=GptJobState.ACTIVE,
    )

    class ActivityCdp(FakeCdp):
        def __init__(self) -> None:
            super().__init__()
            self._targets = [BrowserTarget("managed", "page", "https://chatgpt.com/c/job", "Working", "ws://managed")]

        def chatgpt_companion_state(self, target_id: str):
            return {
                "ghost": False,
                "focused": False,
                "busy": True,
                "composer_chars": 0,
                "user_turns": 1,
                "assistant_turns": 1,
                "tool_activity_count": 0,
                "streaming_current": True,
                "last_assistant_text": "Risposta corrente",
            }

    controller = GptWorkController(queue, ActivityCdp())
    snapshot = controller.dashboard_snapshot()
    current = next(item for item in snapshot["jobs"] if item["job_id"] == job.job_id)
    assert current["live_busy"] is True
    assert current["live_pending"] is False


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
    assert cdp.started[0]["prompt"].count("BOT-TAZZI GOAL LOOP") == 1
    assert "[[BOTTAZZI_GOAL_REACHED]]" in cdp.started[0]["prompt"]
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


def test_dashboard_distinguishes_pending_stop_button_from_real_streaming(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "One",
        conversation_url="https://chatgpt.com/c/chat-1",
        conversation_context_url="https://chatgpt.com/c/chat-1",
        target_id="target-1",
        state=GptJobState.ACTIVE,
    )
    cdp = FakeCdp()
    cdp._targets = [BrowserTarget("target-1", "page", "https://chatgpt.com/c/chat-1", "One", "ws://target-1")]
    cdp.chatgpt_companion_state = lambda target_id: {
        "ghost": False,
        "focused": False,
        "busy": True,
        "streaming_current": False,
        "composer_chars": 0,
        "user_turns": 2,
        "assistant_turns": 1,
        "tool_activity_count": 0,
        "last_assistant_text": "Risposta vecchia",
    }
    controller = GptWorkController(queue, cdp)

    pending = next(item for item in controller.dashboard_snapshot()["jobs"] if item["job_id"] == job.job_id)
    assert pending["live_busy"] is False
    assert pending["live_pending"] is True

    controller._companion_cache.clear()
    cdp.chatgpt_companion_state = lambda target_id: {
        "ghost": False,
        "focused": False,
        "busy": True,
        "streaming_current": True,
        "composer_chars": 0,
        "user_turns": 2,
        "assistant_turns": 2,
        "tool_activity_count": 0,
        "last_assistant_text": "Nuovo testo live",
    }
    streaming = next(item for item in controller.dashboard_snapshot()["jobs"] if item["job_id"] == job.job_id)
    assert streaming["live_busy"] is True
    assert streaming["live_pending"] is False


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


def test_recycle_job_target_preserves_project_context(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Project chat",
        conversation_url="https://chatgpt.com/c/chat-project",
        conversation_context_url="https://chatgpt.com/g/g-p-demo-project/c/chat-project",
        target_id="old-target",
        state=GptJobState.ACTIVE,
    )
    cdp = FakeCdp()
    cdp._targets = [BrowserTarget("old-target", "page", job.conversation_context_url, "Project", "ws://old-target")]
    controller = GptWorkController(queue, cdp)

    result = controller.recycle_job_target(job.job_id)

    rebound = queue.get_job(job.job_id)
    target = next(t for t in cdp._targets if t.target_id == result["new_target_id"])
    assert target.url == "https://chatgpt.com/g/g-p-demo-project/c/chat-project"
    assert cdp.installed[-1] == (result["new_target_id"], "https://chatgpt.com/g/g-p-demo-project/c/chat-project")
    assert rebound.conversation_context_url == "https://chatgpt.com/g/g-p-demo-project/c/chat-project"


def test_restart_job_in_new_chat_preserves_old_server_chat(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Stalled",
        prompt="Completa il lavoro",
        conversation_url="https://chatgpt.com/c/old-chat",
        conversation_context_url="https://chatgpt.com/c/old-chat",
        target_id="old-target",
        state=GptJobState.ACTIVE,
    )
    cdp = FakeCdp()
    cdp._targets = [
        BrowserTarget("old-target", "page", job.conversation_context_url, "Old", "ws://old-target")
    ]
    controller = GptWorkController(queue, cdp)

    result = controller.restart_job_in_new_chat(job.job_id, reason="worker_stalled_after_retries")

    rebound = queue.get_job(job.job_id)
    assert result["action"] == "rolled_over"
    assert result["server_chat_deleted"] is False
    assert result["old_conversation_url"] == "https://chatgpt.com/c/old-chat"
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.conversation_url == "https://chatgpt.com/c/chat-1"
    assert rebound.target_id == "target-1"
    assert "old-target" in cdp.closed
    assert "Non ripartire da zero" in cdp.started[0]["prompt"]
    assert "https://chatgpt.com/c/old-chat" in cdp.started[0]["prompt"]
    assert "Completa il lavoro" in cdp.started[0]["prompt"]


def test_restart_job_in_new_chat_is_transactional_before_rebind(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Stalled",
        prompt="Completa il lavoro",
        conversation_url="https://chatgpt.com/c/old-chat",
        conversation_context_url="https://chatgpt.com/c/old-chat",
        target_id="old-target",
        state=GptJobState.ACTIVE,
    )

    class FailingInstallCdp(FakeCdp):
        def install_human_input_target(self, target_id: str, conversation_url: str):
            if target_id.startswith("target-"):
                raise CdpError("install_failed")
            return super().install_human_input_target(target_id, conversation_url)

    cdp = FailingInstallCdp()
    cdp._targets = [
        BrowserTarget("old-target", "page", job.conversation_context_url, "Old", "ws://old-target")
    ]
    controller = GptWorkController(queue, cdp)

    with pytest.raises(CdpError, match="install_failed"):
        controller.restart_job_in_new_chat(job.job_id, reason="worker_stalled_after_retries")

    preserved = queue.get_job(job.job_id)
    assert preserved.state is GptJobState.ACTIVE
    assert preserved.conversation_url == "https://chatgpt.com/c/old-chat"
    assert preserved.target_id == "old-target"
    assert "old-target" not in cdp.closed
    assert "target-1" in cdp.closed


def test_start_job_recovers_project_chat_on_fresh_target_when_deeplink_fails(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    project_id = "g-p-" + "a" * 32
    context_url = f"https://chatgpt.com/g/{project_id}-demo/c/project-chat"
    job = queue.create_job(
        "Project recovery",
        conversation_url="https://chatgpt.com/c/project-chat",
        conversation_context_url=context_url,
        state=GptJobState.REVIEW,
    )

    class ProjectRecoveryCdp(FakeCdp):
        def navigate_chatgpt_conversation(self, target_id: str, url: str):
            raise CdpError("conversation_project_click_timeout:https://chatgpt.com/c/project-chat")

        def create_project_conversation_target(self, url: str, *, background: bool = True, wait_timeout_s: float = 12.0):
            assert url == context_url
            assert wait_timeout_s == 30.0
            target_id = "fresh-project-target"
            self._targets.append(BrowserTarget(target_id, "page", context_url, "Project recovery", f"ws://{target_id}"))
            return {"new_target_id": target_id, "conversation_context_url": context_url, "recovered_via_project": True}

        def install_human_input_target(self, target_id: str, conversation_url: str):
            raise CdpError("cdp_transport_error:Runtime.evaluate:WebSocketTimeoutException")

    cdp = ProjectRecoveryCdp()
    controller = GptWorkController(queue, cdp)

    result = controller.start_job(job.job_id)

    rebound = queue.get_job(job.job_id)
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.target_id == "fresh-project-target"
    assert rebound.conversation_context_url == context_url
    assert result["errors"] == []
    assert any(target_id.startswith("reopen-") for target_id in cdp.closed)


def test_send_message_rejects_target_reused_for_different_conversation(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job("One", prompt="Do one")
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    controller.pump(max_to_start=1)
    active = queue.get_job(job.job_id)
    assert active.target_id == "target-1"
    assert active.conversation_url == "https://chatgpt.com/c/chat-1"

    cdp._targets = [
        BrowserTarget("target-1", "page", "https://chatgpt.com/c/other-chat", "Other", "ws://target-1"),
    ]

    with pytest.raises(CdpError, match="job_target_assignment_mismatch"):
        controller.send_message(job.job_id, "Non mischiare questa chat")

    assert cdp.messages == []


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


def test_marking_done_immediately_starts_next_queued_job(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    queue.set_max_open_chats(1)
    cdp = FakeCdp()
    with running_frontend(queue, cdp) as (port, origin):
        status, first = request(
            port,
            "POST",
            "/api/jobs",
            origin=origin,
            payload={"title": "First", "prompt": "Do first"},
        )
        assert status == 200
        first_id = first["job"]["job_id"]
        assert len(first["pump"]["started"]) == 1

        status, second = request(
            port,
            "POST",
            "/api/jobs",
            origin=origin,
            payload={"title": "Second", "prompt": "Do second", "auto_start": False},
        )
        assert status == 200
        second_id = second["job"]["job_id"]
        assert queue.get_job(second_id).state is GptJobState.QUEUED

        status, finished = request(
            port,
            "POST",
            f"/api/jobs/{first_id}/state",
            origin=origin,
            payload={"state": "done"},
        )

    assert status == 200
    assert finished["job"]["state"] == "done"
    assert len(finished["start"]["started"]) == 1
    assert finished["start"]["started"][0]["job_id"] == second_id
    assert queue.get_job(second_id).state is GptJobState.ACTIVE


def test_audio_transcription_endpoint_accepts_frontend_audio(tmp_path: Path, monkeypatch) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    monkeypatch.setattr(
        GptWorkController,
        "transcribe_audio",
        lambda self, audio, content_type: {"text": "ciao bottazzi", "engine": "test", "language": "it", "bytes": len(audio)},
    )
    with running_frontend(queue, cdp) as (port, origin):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        body = b"fake-webm-audio"
        conn.request(
            "POST",
            "/api/audio/transcribe",
            body=body,
            headers={
                "Content-Type": "audio/webm",
                "Content-Length": str(len(body)),
                "X-Bottazzi-Frontend": "1",
                "Origin": origin,
            },
        )
        response = conn.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        conn.close()

    assert response.status == 200
    assert data["text"] == "ciao bottazzi"
    assert data["bytes"] == len(body)


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


def test_account_history_lists_existing_chat_without_opening_new_one(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    cdp._targets = [BrowserTarget("worker", "page", "https://chatgpt.com/c/open-1", "Open", "ws://worker")]
    cdp.history_records = [
        {"url": "https://chatgpt.com/c/history-1", "title": "Filologo storico"},
        {"url": "https://chatgpt.com/c/open-1", "title": "Chat aperta"},
    ]
    controller = GptWorkController(queue, cdp)

    history = controller.account_history()

    assert history["error"] is None
    assert [row["title"] for row in history["chats"]] == ["Filologo storico", "Chat aperta"]
    assert history["chats"][0]["open"] is False
    assert history["chats"][1]["open"] is True
    assert cdp.started == []


def test_history_resume_reopens_existing_conversation_without_creating_chat(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    cdp._targets = [BrowserTarget("home", "page", "https://chatgpt.com/", "ChatGPT", "ws://home")]
    cdp.history_records = [{"url": "https://chatgpt.com/c/history-1", "title": "Lavoro vecchio"}]
    with running_frontend(queue, cdp) as (port, origin):
        status, history = request(port, "GET", "/api/history", origin=origin)
        assert status == 200
        assert history["chats"][0]["conversation_url"] == "https://chatgpt.com/c/history-1"

        status, resumed = request(
            port,
            "POST",
            "/api/history/resume",
            origin=origin,
            payload={"conversation_url": "https://chatgpt.com/c/history-1", "title": "Lavoro vecchio"},
        )

    assert status == 200
    assert resumed["server_chat_created"] is False
    assert resumed["job"]["conversation_url"] == "https://chatgpt.com/c/history-1"
    assert resumed["job"]["state"] == "active"
    assert cdp.started == []
    assert any(target.url == "https://chatgpt.com/c/history-1" for target in cdp._targets)
    assert cdp.installed[-1][1] == "https://chatgpt.com/c/history-1"


def test_account_history_exposes_projects_and_project_chat_context(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    cdp._targets = [BrowserTarget("home", "page", "https://chatgpt.com/", "ChatGPT", "ws://home")]
    cdp.history_projects = [
        {"url": "https://chatgpt.com/g/g-p-colletti/project", "title": "Colletti Bianchi", "project_id": "g-p-colletti"},
    ]
    cdp.history_records = [
        {
            "url": "https://chatgpt.com/c/history-1",
            "context_url": "https://chatgpt.com/g/g-p-colletti/c/history-1",
            "title": "Commercialista",
            "project_id": "g-p-colletti",
        },
    ]
    controller = GptWorkController(queue, cdp)

    history = controller.account_history()

    assert history["project_count"] == 1
    assert history["projects"][0]["title"] == "Colletti Bianchi"
    assert history["chats"][0]["project_name"] == "Colletti Bianchi"
    assert history["chats"][0]["project_url"] == "https://chatgpt.com/g/g-p-colletti/project"
    assert history["chats"][0]["conversation_context_url"] == "https://chatgpt.com/g/g-p-colletti/c/history-1"


def test_project_history_resume_preserves_project_context_without_new_chat(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    cdp._targets = [BrowserTarget("home", "page", "https://chatgpt.com/", "ChatGPT", "ws://home")]
    with running_frontend(queue, cdp) as (port, origin):
        status, resumed = request(
            port,
            "POST",
            "/api/history/resume",
            origin=origin,
            payload={
                "conversation_url": "https://chatgpt.com/c/history-project-1",
                "conversation_context_url": "https://chatgpt.com/g/g-p-colletti/c/history-project-1",
                "title": "Commercialista",
                "project_name": "Colletti Bianchi",
                "project_url": "https://chatgpt.com/g/g-p-colletti/project",
            },
        )

    assert status == 200
    assert resumed["server_chat_created"] is False
    assert resumed["job"]["project_name"] == "Colletti Bianchi"
    assert resumed["job"]["project_url"] == "https://chatgpt.com/g/g-p-colletti/project"
    assert resumed["job"]["conversation_context_url"] == "https://chatgpt.com/g/g-p-colletti/c/history-project-1"
    assert cdp.started == []


def test_create_job_resolves_unique_name_from_native_catalog(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    cdp._targets.append(BrowserTarget("catalog", "page", "https://chatgpt.com/", "ChatGPT", "ws://catalog"))
    cdp.resolved_project_urls["Indipendentemenza dai colletti bianchi"] = "https://chatgpt.com/g/g-p-colletti/project"
    with running_frontend(queue, cdp) as (port, origin):
        status, created = request(
            port,
            "POST",
            "/api/jobs",
            origin=origin,
            payload={
                "title": "Filologo",
                "prompt": "Continua il lavoro",
                "project_name": "Indipendentemenza dai colletti bianchi",
                "project_url": None,
                "auto_start": False,
            },
        )

    assert status == 200
    assert created["job"]["project_name"] == "Indipendentemenza dai colletti bianchi"
    assert created["job"]["project_url"] == "https://chatgpt.com/g/g-p-colletti/project"
    assert cdp.closed == []
    assert not any(target.target_id.startswith("temp-") for target in cdp._targets)


def test_frontend_rejects_client_outside_allowed_networks(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    server = create_server(
        queue,
        cdp,
        origin=origin,
        allowed_networks=("10.252.14.0/24",),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = request(port, "GET", "/healthz", origin=origin)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 403
    assert body["error"] == "client_not_allowed"


def test_frontend_accepts_vpn_host_when_explicitly_allowed(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    cdp = FakeCdp()
    port = free_port()
    local_origin = f"http://127.0.0.1:{port}"
    vpn_origin = f"http://10.252.14.7:{port}"
    server = create_server(
        queue,
        cdp,
        origin=local_origin,
        allowed_origins=(local_origin, vpn_origin),
        allowed_networks=("127.0.0.0/8", "10.252.14.0/24"),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = request(
            port,
            "GET",
            "/healthz",
            origin=local_origin,
            extra_headers={"Host": f"10.252.14.7:{port}"},
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert status == 200
    assert body["ok"] is True

def test_start_job_recovers_plain_chat_via_sidebar_when_deeplink_fails(tmp_path: Path) -> None:
    queue = make_queue(tmp_path)
    job = queue.create_job(
        "Plain recovery",
        conversation_url="https://chatgpt.com/c/plain-chat",
        conversation_context_url="https://chatgpt.com/c/plain-chat",
        state=GptJobState.REVIEW,
    )

    class SidebarRecoveryCdp(FakeCdp):
        def navigate_chatgpt_conversation(self, target_id: str, url: str):
            raise CdpError("conversation_navigation_timeout:https://chatgpt.com/c/plain-chat")

        def create_sidebar_conversation_target(self, url: str, *, background: bool = True, wait_timeout_s: float = 30.0):
            assert url == "https://chatgpt.com/c/plain-chat"
            assert wait_timeout_s == 30.0
            target_id = "fresh-sidebar-target"
            self._targets.append(BrowserTarget(target_id, "page", url, "Plain recovery", f"ws://{target_id}"))
            return {"new_target_id": target_id, "conversation_context_url": url, "recovered_via_sidebar": True}

    cdp = SidebarRecoveryCdp()
    controller = GptWorkController(queue, cdp)

    result = controller.start_job(job.job_id)

    rebound = queue.get_job(job.job_id)
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.target_id == "fresh-sidebar-target"
    assert rebound.conversation_context_url == "https://chatgpt.com/c/plain-chat"
    assert result["errors"] == []
    assert any(target_id.startswith("reopen-") for target_id in cdp.closed)
