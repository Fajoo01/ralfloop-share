from __future__ import annotations

from pathlib import Path

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget
from ralfloop_agent.integration.gpt_queue_shepherd import (
    GptQueueShepherd,
    GptQueueShepherdPolicy,
)
from ralfloop_agent.integration.gpt_work_queue import GptJobState, GptWorkQueue


class FakeCdp:
    def __init__(self, *, ui: dict, companion: dict | None = None) -> None:
        self.endpoint = "http://127.0.0.1:9238"
        self.ui = dict(ui)
        self.companion = {"focused": False, "composer_chars": 0, **(companion or {})}
        self.closed: list[str] = []
        self._targets = [
            BrowserTarget(
                "managed",
                "page",
                "https://chatgpt.com/c/job",
                "Managed",
                "ws://managed",
            ),
            BrowserTarget(
                "unmanaged",
                "page",
                "https://chatgpt.com/c/other",
                "Unmanaged",
                "ws://unmanaged",
            ),
        ]

    def targets(self):
        return list(self._targets)

    def chatgpt_ui_state(self, target_id: str):
        assert target_id == "managed"
        return dict(self.ui)

    def chatgpt_companion_state(self, target_id: str):
        assert target_id == "managed"
        return dict(self.companion)

    def close_target(self, target_id: str):
        self.closed.append(target_id)
        self._targets = [target for target in self._targets if target.target_id != target_id]


def queue_with_active(tmp_path: Path) -> tuple[GptWorkQueue, str]:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)
    job = queue.create_job(
        "Managed",
        conversation_url="https://chatgpt.com/c/job",
        conversation_context_url="https://chatgpt.com/c/job",
        target_id="managed",
        state=GptJobState.ACTIVE,
    )
    return queue, job.job_id


def shepherd(queue: GptWorkQueue, cdp: FakeCdp) -> GptQueueShepherd:
    return GptQueueShepherd(
        queue,
        cdp,
        policy=GptQueueShepherdPolicy(
            complete_idle_ms=60_000,
            stalled_idle_ms=180_000,
        ),
        completion_notifier=lambda title: {"ok": True, "title": title},
    )


def test_fresh_not_pending_reply_waits_for_stable_idle_window(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 0,
        },
        companion={"busy": False, "last_assistant_text": "Va bene. Quando Bruto è"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert report["actions"][0]["reason"] == "awaiting_settle"


def test_completed_reply_with_stale_pending_is_released(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": True,
            "response_idle_ms": 61_000,
        },
        companion={"busy": False, "last_assistant_text": "Risposta finale stabile."},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    job = queue.get_job(job_id)
    assert job.state is GptJobState.REVIEW
    assert job.target_id is None
    assert cdp.closed == ["managed"]
    assert report["actions"][0]["reason"] == "response_complete"
    assert any(target.target_id == "unmanaged" for target in cdp.targets())


def test_streaming_reply_is_never_released(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": True,
            "response_pending": True,
            "response_idle_ms": 999_999,
        }
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert report["actions"][0]["reason"] == "response_in_progress"


def test_companion_busy_is_preserved_while_response_is_still_fresh(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": True,
            "response_idle_ms": 10_000,
        },
        companion={"busy": True, "last_assistant_text": "Sì. Per"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert report["actions"][0]["reason"] == "companion_busy"


def test_stale_busy_flag_releases_stable_substantive_answer(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    final = "Va bene. Quando Bruto è di nuovo acceso e raggiungibile, riprendo da lì senza rifare i passaggi già completati."
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": True,
            "response_idle_ms": 61_000,
        },
        companion={"busy": True, "last_assistant_text": final},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    job = queue.get_job(job_id)
    assert job.state is GptJobState.REVIEW
    assert job.last_assistant_text == final
    assert cdp.closed == ["managed"]
    assert report["actions"][0]["reason"] == "response_complete"


def test_transient_thinking_text_is_not_a_completed_answer(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    queue.set_last_assistant_text(job_id, "Sto pensando")
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": True,
            "response_idle_ms": 61_000,
        },
        companion={"busy": False, "last_assistant_text": "Sto pensando"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert report["actions"][0]["reason"] == "awaiting_settle"


def test_focused_chat_is_never_released(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 999_999,
        },
        companion={"focused": True},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert report["actions"][0]["reason"] == "focused"


def test_stalled_unanswered_job_moves_to_review_with_error(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": True,
            "response_idle_ms": 181_000,
        }
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    job = queue.get_job(job_id)
    assert job.state is GptJobState.REVIEW
    assert job.target_id is None
    assert job.last_error == "worker_stalled_without_reply"
    assert cdp.closed == ["managed"]
    assert report["actions"][0]["reason"] == "worker_stalled_without_reply"


def test_nonempty_composer_is_never_released(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 999_999,
        },
        companion={"composer_chars": 12},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert report["actions"][0]["reason"] == "composer_not_empty"
