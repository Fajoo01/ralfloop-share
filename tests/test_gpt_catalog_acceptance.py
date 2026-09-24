"""Independent regressions for safe history reopening and project context."""
from pathlib import Path

import pytest

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, CdpError
from ralfloop_agent.integration.gpt_frontend import GptWorkController
from ralfloop_agent.integration.gpt_work_queue import GptJobState, GptWorkQueue
from test_gpt_frontend import FakeCdp


CANONICAL = "https://chatgpt.com/c/old-chat"
CONTEXT = "https://chatgpt.com/g/g-p-existing/c/old-chat"
PROJECT = "https://chatgpt.com/g/g-p-existing/project"


@pytest.mark.parametrize("operation", ["create", "bind"])
def test_context_must_reference_the_bound_conversation(tmp_path: Path, operation: str) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3")
    mismatched = "https://chatgpt.com/g/g-p-existing/c/different-chat"
    with pytest.raises(ValueError):
        if operation == "create":
            queue.create_job("Existing", conversation_url=CANONICAL, conversation_context_url=mismatched)
        else:
            job = queue.create_job("Existing", conversation_url=CANONICAL)
            queue.bind_chat(job.job_id, conversation_url=CANONICAL,
                            conversation_context_url=mismatched, target_id="old-target")


@pytest.mark.parametrize("state", [GptJobState.DONE, GptJobState.CANCELLED])
def test_resume_terminal_history_job_reuses_persistent_identity(tmp_path: Path, state: GptJobState) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3")
    original = queue.create_job("Existing", conversation_url=CANONICAL,
                                conversation_context_url=CONTEXT, project_url=PROJECT, state=state)
    cdp = FakeCdp()
    result = GptWorkController(queue, cdp).resume_history_chat(CANONICAL,
                conversation_context_url=CONTEXT, project_url=PROJECT)
    assert result["job"]["job_id"] == original.job_id
    assert result["job"]["state"] == "active"
    assert result["job"]["conversation_context_url"] == CONTEXT
    assert result["server_chat_created"] is False
    assert cdp.started == []


def test_reconcile_canonical_tab_preserves_known_project_context(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3")
    job = queue.create_job("Existing", conversation_url=CANONICAL, conversation_context_url=CONTEXT,
                           project_url=PROJECT, target_id="old-target", state=GptJobState.ACTIVE)
    cdp = FakeCdp()
    cdp._targets = [BrowserTarget("replacement", "page", CANONICAL, "Existing", "ws://replacement")]
    GptWorkController(queue, cdp).reconcile()
    rebound = queue.get_job(job.job_id)
    assert rebound.target_id == "replacement"
    assert rebound.conversation_context_url == CONTEXT
    assert rebound.conversation_url == CANONICAL


def test_resume_rejects_mismatched_context_before_browser_navigation(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3")
    cdp = FakeCdp()
    with pytest.raises(ValueError):
        GptWorkController(queue, cdp).resume_history_chat(CANONICAL,
            conversation_context_url="https://chatgpt.com/g/g-p-existing/c/different-chat")
    assert cdp._targets == []
    assert queue.list_jobs() == []


def test_resume_upgrades_existing_job_with_discovered_project_context(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3")
    original = queue.create_job("Existing", conversation_url=CANONICAL, state=GptJobState.REVIEW)
    cdp = FakeCdp()
    result = GptWorkController(queue, cdp).resume_history_chat(CANONICAL,
        conversation_context_url=CONTEXT, project_url=PROJECT, project_name="Existing project")
    resumed = result["job"]
    assert resumed["job_id"] == original.job_id
    assert resumed["conversation_context_url"] == CONTEXT
    assert resumed["project_url"] == PROJECT
    assert resumed["project_name"] == "Existing project"
    assert any(target.url == CONTEXT for target in cdp._targets)
    assert cdp.started == []


def test_project_catalog_rewrites_context_to_real_project_slug(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3")
    cdp = FakeCdp()
    cdp._targets = [BrowserTarget("home", "page", "https://chatgpt.com/", "ChatGPT", "ws://home")]
    project_url = "https://chatgpt.com/g/g-p-existing-real-project/project"
    full_context = "https://chatgpt.com/g/g-p-existing-real-project/c/old-chat"
    cdp.history_projects = [
        {"url": project_url, "title": "Existing project", "project_id": "g-p-existing"},
    ]
    cdp.history_records = [
        {"url": CANONICAL, "context_url": CONTEXT, "title": "Existing", "project_id": "g-p-existing"},
    ]

    history = GptWorkController(queue, cdp).account_history(project_id="g-p-existing")

    assert history["error"] is None
    assert history["chats"][0]["conversation_context_url"] == full_context
    assert history["chats"][0]["project_url"] == project_url
    assert any(target_id.startswith("temp-") for target_id in cdp.closed)


def test_rate_limited_history_resume_keeps_exact_target_review_and_send_disabled(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3")
    cdp = FakeCdp()
    controller = GptWorkController(queue, cdp)
    project_url = "https://chatgpt.com/g/g-p-existing-real-project/project"
    full_context = "https://chatgpt.com/g/g-p-existing-real-project/c/old-chat"
    original_navigate = cdp.navigate_chatgpt_conversation

    def limited_navigate(target_id: str, url: str):
        original_navigate(target_id, url)
        raise CdpError("temporary_access_limited")

    cdp.navigate_chatgpt_conversation = limited_navigate
    cdp.chatgpt_ui_state = lambda target_id: {"temporary_access_limited": True}

    result = controller.resume_history_chat(
        CANONICAL,
        conversation_context_url=full_context,
        project_name="Existing project",
        project_url=project_url,
    )

    job = queue.get_job(result["job"]["job_id"])
    target = next(target for target in cdp._targets if target.target_id == job.target_id)
    assert job.state is GptJobState.REVIEW
    assert job.last_error == "temporary_access_limited"
    assert target.url == full_context
    assert result["start"]["errors"] == [{"job_id": job.job_id, "error": "temporary_access_limited"}]
    assert result["start"]["opened"][0]["target_id"] == job.target_id
    assert cdp.installed == []
    with pytest.raises(ValueError, match="temporary_access_limited"):
        controller.send_message(job.job_id, "must not send")
    assert cdp.messages == []
    controller.reconcile()
    assert queue.get_job(job.job_id).state is GptJobState.REVIEW
