from __future__ import annotations

from pathlib import Path

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, ChromeCdp
from ralfloop_agent.integration.gpt_work_queue import GptJobState, GptWorkQueue


def queue(tmp_path: Path) -> GptWorkQueue:
    return GptWorkQueue(tmp_path / "gpt-work-queue.sqlite3", clock=lambda: 1_000_000)


def test_queue_add_reorder_and_project_sections(tmp_path: Path) -> None:
    q = queue(tmp_path)
    first = q.create_job(
        "BaffoFlix",
        prompt="Testa YouTube",
        project_name="BaffoFlix",
        project_url="https://chatgpt.com/g/g-p-baffo/project",
    )
    second = q.create_job("Controlla browser", prompt="Controlla browser GPT")
    third = q.create_job(
        "Filologia",
        prompt="Riprendi Scholarly",
        project_name="Indipendenza dai colletti bianchi",
        project_url="https://chatgpt.com/g/g-p-white/c/abc",
    )

    q.reorder(third.job_id, 1)
    jobs = q.list_jobs()

    assert [job.job_id for job in jobs] == [third.job_id, first.job_id, second.job_id]
    snapshot = q.snapshot()
    assert snapshot["settings"]["max_open_chats"] == 3
    by_name = {section["project_name"]: section for section in snapshot["projects"]}
    assert by_name["Indipendenza dai colletti bianchi"]["project_url"] == "https://chatgpt.com/g/g-p-white/project"
    assert by_name["BaffoFlix"]["job_ids"] == [first.job_id]
    assert by_name["Senza progetto"]["job_ids"] == [second.job_id]


def test_queue_persists_slot_limit_and_chat_binding(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    q = GptWorkQueue(path, clock=lambda: 1_000_000)
    job = q.create_job("Apri chat", prompt="Fai il lavoro")
    q.set_max_open_chats(4)
    q.bind_chat(
        job.job_id,
        conversation_url="https://chatgpt.com/g/g-p-demo/c/abcdef",
        target_id="target-1",
        state=GptJobState.ACTIVE,
    )

    second = GptWorkQueue(path, clock=lambda: 1_000_100)
    loaded = second.get_job(job.job_id)

    assert second.settings().max_open_chats == 4
    assert loaded.state is GptJobState.ACTIVE
    assert loaded.conversation_url == "https://chatgpt.com/c/abcdef"
    assert loaded.target_id == "target-1"


def test_watchdog_recovery_counter_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    first = GptWorkQueue(path, clock=lambda: 1_000_000)
    job = first.create_job("Watchdog", prompt="continua")
    assert first.watchdog_state(job.job_id)["recovery_count"] == 0
    first.mark_watchdog_recovery(job.job_id)
    second = GptWorkQueue(path, clock=lambda: 1_000_100)
    assert second.watchdog_state(job.job_id) == {
        "recovery_count": 1,
        "last_recovery_at": 1_000_000,
        "transport_failure_count": 0,
        "last_transport_failure_at": 0,
    }
    transport = second.mark_watchdog_transport_failure(job.job_id)
    assert transport["recovery_count"] == 1
    assert transport["transport_failure_count"] == 1
    second.reset_watchdog_transport_failures(job.job_id)
    assert second.watchdog_state(job.job_id)["transport_failure_count"] == 0
    second.reset_watchdog(job.job_id)
    assert first.watchdog_state(job.job_id)["recovery_count"] == 0


def test_goal_continue_baseline_is_durable_per_assistant_turn(tmp_path: Path) -> None:
    path = tmp_path / "queue.sqlite3"
    first = GptWorkQueue(path, clock=lambda: 1_000_000)
    job = first.create_job("Goal loop", state=GptJobState.ACTIVE)
    first.set_goal_continue_baseline(job.job_id, assistant_turns=4, assistant_text="Fase completata. [[BOTTAZZI_GOAL_CONTINUE]]")

    second = GptWorkQueue(path, clock=lambda: 1_000_015)
    state = second.goal_continue_state(job.job_id)

    assert state["assistant_turns"] == 4
    assert state["sent_at"] == 1_000_000
    assert second.goal_continue_already_sent(job.job_id, assistant_turns=4, assistant_text="Fase completata. [[BOTTAZZI_GOAL_CONTINUE]]")
    assert not second.goal_continue_already_sent(job.job_id, assistant_turns=5, assistant_text="Fase completata. [[BOTTAZZI_GOAL_CONTINUE]]")
    assert not second.goal_continue_already_sent(job.job_id, assistant_turns=4, assistant_text="Nuova fase. [[BOTTAZZI_GOAL_CONTINUE]]")


def test_terminal_job_leaves_active_queue(tmp_path: Path) -> None:
    q = queue(tmp_path)
    first = q.create_job("Uno", prompt="1")
    second = q.create_job("Due", prompt="2")

    q.set_state(first.job_id, GptJobState.DONE)

    assert [job.job_id for job in q.list_jobs()] == [second.job_id]
    assert {job.job_id for job in q.list_jobs(include_terminal=True)} == {first.job_id, second.job_id}


class FakeJobCdp(ChromeCdp):
    def __init__(self, *, fail: bool = False) -> None:
        super().__init__("http://127.0.0.1:9238")
        self.fail = fail
        self.closed: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.target = BrowserTarget("new", "page", "about:blank", "", "ws://new")

    def create_target(self, url: str, *, background: bool = False) -> str:
        self.calls.append(("create", f"{url}|background={background}"))
        return "new"

    def _wait_target(self, target_id: str, *, timeout_s: float = 10.0) -> BrowserTarget:
        assert target_id == "new"
        return self.target

    def _page_call(self, websocket_url: str, method: str, params=None, *, timeout_s=None):
        self.calls.append((method, str((params or {}).get("url") or "")))
        if method == "Page.navigate":
            url = str((params or {}).get("url") or "")
            self.target = BrowserTarget("new", "page", url, "", "ws://new")
        return {}

    def inject_prompt(self, prompt: str, *, target_id: str | None = None, submit: bool = False, wait_timeout_s: float = 20.0):
        assert target_id == "new"
        assert submit is True
        if self.fail:
            raise RuntimeError("boom")
        self.target = BrowserTarget(
            "new",
            "page",
            "https://chatgpt.com/g/g-p-demo/c/fresh-chat",
            "fresh",
            "ws://new",
        )
        return {"target_id": "new", "injected": True, "submitted": True, "submit_confirmed": True, "submit_method": "button_js"}

    def close_target(self, target_id: str) -> None:
        self.closed.append(target_id)


def test_start_chatgpt_job_uses_project_and_returns_conversation() -> None:
    cdp = FakeJobCdp()

    result = cdp.start_chatgpt_job(
        "Fai il lavoro",
        new_chat_url="https://chatgpt.com/g/g-p-demo/project",
        background=True,
        submit=True,
    )

    assert result["new_target_id"] == "new"
    assert result["conversation_url"] == "https://chatgpt.com/c/fresh-chat"
    assert result["conversation_context_url"] == "https://chatgpt.com/g/g-p-demo/c/fresh-chat"
    assert ("Page.navigate", "https://chatgpt.com/g/g-p-demo/project") in cdp.calls
    assert cdp.closed == []


def test_start_chatgpt_job_closes_only_new_target_on_failure() -> None:
    cdp = FakeJobCdp(fail=True)

    try:
        cdp.start_chatgpt_job("Fai il lavoro")
    except RuntimeError as exc:
        assert str(exc) == "boom"
    else:
        raise AssertionError("expected RuntimeError")

    assert cdp.closed == ["new"]
