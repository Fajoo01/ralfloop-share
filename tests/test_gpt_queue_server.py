from __future__ import annotations

from pathlib import Path

import pytest

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, CdpError
from ralfloop_agent.integration.gpt_frontend import GptWorkController
from ralfloop_agent.integration.gpt_work_queue import GptJobState, GptWorkQueue


class FakeCdp:
    def __init__(self, targets: list[BrowserTarget] | None = None) -> None:
        self.endpoint = "http://127.0.0.1:9238"
        self._targets = list(targets or [])
        self.installs: list[tuple[str, str]] = []
        self.messages: list[tuple[str, str, str]] = []
        self.closed: list[str] = []
        self.activated: list[str] = []

    def targets(self):
        return list(self._targets)

    def chatgpt_companion_state(self, target_id: str):
        return {"ghost": False, "focused": False, "busy": False, "composer_chars": 0}

    def install_human_input_target(self, target_id: str, conversation_url: str):
        self.installs.append((target_id, conversation_url))
        return {"ok": True}

    def queue_human_message(self, target_id: str, conversation_url: str, text: str):
        self.messages.append((target_id, conversation_url, text))
        return {"ok": True, "queued": True}

    def close_target(self, target_id: str):
        self.closed.append(target_id)
        self._targets = [target for target in self._targets if target.target_id != target_id]

    def activate_target(self, target_id: str):
        self.activated.append(target_id)


def q(tmp_path: Path) -> GptWorkQueue:
    return GptWorkQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)


def page(target_id: str, url: str, title: str = "Chat") -> BrowserTarget:
    return BrowserTarget(target_id, "page", url, title, f"ws://{target_id}")


def test_dashboard_slots_ignore_unmanaged_old_tabs(tmp_path: Path) -> None:
    queue = q(tmp_path)
    job = queue.create_job(
        "Gestita",
        conversation_url="https://chatgpt.com/c/a",
        target_id="managed",
        state=GptJobState.ACTIVE,
    )
    cdp = FakeCdp([
        page("managed", "https://chatgpt.com/c/a", "Gestita"),
        page("external", "https://chatgpt.com/c/b", "Esterna"),
    ])
    controller = GptWorkController(queue, cdp)

    data = controller.dashboard_snapshot()

    assert data["browser"]["open_chat_count"] == 2
    assert data["browser"]["managed_open_count"] == 1
    assert data["browser"]["unmanaged_open_count"] == 1
    managed = next(row for row in data["browser"]["open_chats"] if row["target_id"] == "managed")
    assert managed["job_id"] == job.job_id


def test_message_fails_closed_if_target_was_navigated_elsewhere(tmp_path: Path) -> None:
    queue = q(tmp_path)
    job = queue.create_job(
        "A",
        conversation_url="https://chatgpt.com/c/a",
        target_id="same-target",
        state=GptJobState.ACTIVE,
    )
    cdp = FakeCdp([page("same-target", "https://chatgpt.com/c/b")])
    controller = GptWorkController(queue, cdp)

    with pytest.raises(CdpError, match="job_target_assignment_mismatch"):
        controller.send_message(job.job_id, "ciao")

    assert cdp.messages == []


def test_message_uses_server_side_project_context_binding(tmp_path: Path) -> None:
    queue = q(tmp_path)
    job = queue.create_job(
        "A",
        conversation_url="https://chatgpt.com/g/g-p-demo/c/a",
        conversation_context_url="https://chatgpt.com/g/g-p-demo/c/a",
        target_id="target-a",
        state=GptJobState.ACTIVE,
    )
    cdp = FakeCdp([page("target-a", "https://chatgpt.com/g/g-p-demo/c/a")])
    controller = GptWorkController(queue, cdp)

    result = controller.send_message(job.job_id, "messaggio A")

    assert result["action"] == "queued"
    assert cdp.installs == [("target-a", "https://chatgpt.com/g/g-p-demo/c/a")]
    assert cdp.messages == [("target-a", "https://chatgpt.com/g/g-p-demo/c/a", "messaggio A")]


def test_duplicate_conversation_targets_are_not_silently_rebound(tmp_path: Path) -> None:
    queue = q(tmp_path)
    job = queue.create_job(
        "A",
        conversation_url="https://chatgpt.com/c/a",
        target_id="missing-old-target",
        state=GptJobState.ACTIVE,
    )
    cdp = FakeCdp([
        page("dup-1", "https://chatgpt.com/c/a"),
        page("dup-2", "https://chatgpt.com/c/a"),
    ])
    controller = GptWorkController(queue, cdp)

    controller.reconcile()

    reviewed = queue.get_job(job.job_id)
    assert reviewed.state is GptJobState.REVIEW
    assert reviewed.target_id is None
    assert reviewed.last_error == "chat_target_ambiguous"


def test_import_then_finish_closes_only_local_target(tmp_path: Path) -> None:
    queue = q(tmp_path)
    cdp = FakeCdp([page("external", "https://chatgpt.com/g/g-p-demo/c/a", "Demo")])
    controller = GptWorkController(queue, cdp)

    job = controller.import_target("external")
    finished = controller.finish_job(job.job_id, GptJobState.DONE)

    assert job.state is GptJobState.ACTIVE
    assert finished.state is GptJobState.DONE
    assert cdp.closed == ["external"]
    assert queue.get_job(job.job_id).conversation_url == "https://chatgpt.com/c/a"


def test_import_revives_terminal_record_for_same_conversation(tmp_path: Path) -> None:
    queue = q(tmp_path)
    old = queue.create_job(
        "Iter di recupero",
        conversation_url="https://chatgpt.com/c/a",
        conversation_context_url="https://chatgpt.com/c/a",
        state=GptJobState.DONE,
    )
    cdp = FakeCdp([page("external", "https://chatgpt.com/c/a", "Iter di recupero")])
    controller = GptWorkController(queue, cdp)

    revived = controller.import_target("external")

    assert revived.job_id == old.job_id
    assert revived.state is GptJobState.ACTIVE
    assert revived.target_id == "external"
    assert revived.last_error is None
    assert len([job for job in queue.list_jobs(include_terminal=True) if job.conversation_url == "https://chatgpt.com/c/a"]) == 1
