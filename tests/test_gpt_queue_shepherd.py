from __future__ import annotations

from pathlib import Path

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, CdpError
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
        self.messages: list[tuple[str, str, str]] = []
        self.submitted_composers: list[str] = []
        self.wake_calls: list[str] = []
        self.stopped: list[str] = []
        self.holds: list[tuple[str, bool]] = []
        self.created = 0
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

    def install_human_input_target(self, target_id: str, conversation_url: str):
        return {"ok": True}

    def queue_human_message(self, target_id: str, conversation_url: str, text: str):
        self.messages.append((target_id, conversation_url, text))
        return {"queued": True}

    def submit_chatgpt_composer(self, target_id: str, *, wait_timeout_s: float = 8.0):
        self.submitted_composers.append(target_id)
        return {"submitted": True, "confirmed": True}

    def wake_stalled_chatgpt(self, target_id: str, *, text: str = "A che punto sei? Hai risolto?", wait_timeout_s: float = 4.0):
        self.wake_calls.append(target_id)
        return {"submitted": False, "reason": "send_missing_after_wake"}

    def set_human_queue_hold(self, target_id: str, held: bool):
        self.holds.append((target_id, held))
        return {"ok": True, "held": held}

    def stop_chatgpt_response(self, target_id: str):
        self.stopped.append(target_id)
        return {"stopped": True, "last_assistant_text": self.companion.get("last_assistant_text", "")}

    def create_chatgpt_target(self, *, clear_cache: bool = False, background: bool = True):
        self.created += 1
        target_id = f"fresh-{self.created}"
        self._targets.append(BrowserTarget(target_id, "page", "https://chatgpt.com/", "Fresh", f"ws://{target_id}"))
        return target_id

    def navigate_chatgpt_conversation(self, target_id: str, url: str):
        for index, target in enumerate(self._targets):
            if target.target_id == target_id:
                self._targets[index] = BrowserTarget(target_id, "page", url, "Fresh", target.websocket_url)
                return {"authenticated": True, "ready": True}
        raise RuntimeError("missing_target")


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
        companion={"busy": False, "last_assistant_text": "Risposta finale stabile.\n[[BOTTAZZI_GOAL_REACHED]]"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    job = queue.get_job(job_id)
    assert job.state is GptJobState.REVIEW
    assert job.target_id is None
    assert cdp.closed == ["managed"]
    assert report["actions"][0]["reason"] == "goal_complete"
    assert any(target.target_id == "unmanaged" for target in cdp.targets())


def test_review_content_unavailable_rolls_into_new_chat_without_human_wakeup(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)
    job = queue.create_job(
        "Managed",
        prompt="Completa il GOAL",
        conversation_url="https://chatgpt.com/c/old-job",
        conversation_context_url="https://chatgpt.com/c/old-job",
        state=GptJobState.REVIEW,
    )
    queue.set_state(
        job.job_id,
        GptJobState.REVIEW,
        last_error="conversation_content_unavailable_after_retries",
    )

    class RolloverCdp(FakeCdp):
        def start_chatgpt_job(
            self,
            prompt: str,
            *,
            new_chat_url: str,
            background: bool,
            submit: bool,
            wait_timeout_s: float = 30.0,
        ):
            self._targets.append(
                BrowserTarget(
                    "rolled",
                    "page",
                    "https://chatgpt.com/c/rolled",
                    "Rolled",
                    "ws://rolled",
                )
            )
            self.ui = {
                "user_turns": 1,
                "assistant_turns": 0,
                "response_in_progress": True,
                "response_pending": True,
                "response_idle_ms": 1_000,
                "progress_idle_ms": 1_000,
                "tool_activity_count": 0,
            }
            self.companion = {
                "focused": False,
                "composer_chars": 0,
                "busy": True,
                "last_assistant_text": "",
            }
            return {
                "new_target_id": "rolled",
                "conversation_url": "https://chatgpt.com/c/rolled",
                "conversation_context_url": "https://chatgpt.com/c/rolled",
                "server_chat_deleted": False,
            }

        def chatgpt_ui_state(self, target_id: str):
            assert target_id == "rolled"
            return dict(self.ui)

        def chatgpt_companion_state(self, target_id: str):
            assert target_id == "rolled"
            return dict(self.companion)

    cdp = RolloverCdp(ui={})
    cdp._targets = [target for target in cdp._targets if target.target_id != "managed"]
    runner = shepherd(queue, cdp)

    report = runner.run_once(auto_start=False)

    rebound = queue.get_job(job.job_id)
    assert rebound.state is GptJobState.ACTIVE
    assert rebound.conversation_url == "https://chatgpt.com/c/rolled"
    assert rebound.target_id == "rolled"
    assert any(
        action.get("reason") == "review_rollover_new_chat"
        for action in report["actions"]
    )


def test_closed_active_chat_is_held_for_manual_resume(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)

    class ReopenCdp(FakeCdp):
        def chatgpt_ui_state(self, target_id: str):
            return {
                "user_turns": 1,
                "assistant_turns": 1,
                "response_in_progress": False,
                "response_pending": False,
                "response_idle_ms": 1_000,
                "progress_idle_ms": 1_000,
                "temporary_access_limited": False,
            }

        def chatgpt_companion_state(self, target_id: str):
            return {"focused": False, "busy": False, "composer_chars": 0, "last_assistant_text": "Risposta precedente"}

    cdp = ReopenCdp(ui={})
    cdp._targets = [target for target in cdp._targets if target.target_id != "managed"]

    report = shepherd(queue, cdp).run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.target_id is None
    assert saved.last_error == "manual_close_hold"
    assert cdp.created == 0
    assert report["actions"][0]["reason"] == "manual_close_hold"


def test_temporary_access_limit_starts_global_backoff_without_prompting(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 240_000,
            "progress_idle_ms": 240_000,
            "temporary_access_limited": True,
        },
        companion={"busy": False, "last_assistant_text": "Risposta precedente"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.messages == []
    assert cdp.wake_calls == []
    assert cdp.stopped == []
    assert cdp.holds == [("managed", True)]
    assert report["actions"][0]["reason"] == "temporary_access_limited_backoff"
    pacing = queue.automation_pacing_state()
    assert pacing["rate_limit_count"] == 1
    assert pacing["rate_limit_active"] == 1
    assert pacing["backoff_until"] == 1_000_300


def test_rate_limit_backoff_persists_after_banner_disappears(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    queue.note_temporary_access_limit(initial_backoff_s=300, max_backoff_s=3600)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 240_000,
            "progress_idle_ms": 240_000,
            "temporary_access_limited": False,
        },
        companion={"busy": False, "last_assistant_text": "Risposta precedente"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.messages == []
    assert cdp.wake_calls == []
    assert cdp.stopped == []
    assert report["actions"][0]["reason"] == "rate_limit_backoff"
    assert report["actions"][0]["backoff_until"] == 1_000_300


def test_fresh_streaming_reply_is_never_recovered(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": True,
            "response_pending": True,
            "response_idle_ms": 1_000,
            "progress_idle_ms": 1_000,
        },
        companion={"busy": True, "last_assistant_text": "Risposta parziale in avanzamento"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert cdp.stopped == []
    assert report["actions"][0]["reason"] == "response_in_progress"


def test_silent_stream_is_recovered_before_full_stall_timeout(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 0,
            "response_in_progress": True,
            "response_pending": True,
            "response_idle_ms": 121_000,
            "progress_idle_ms": 121_000,
            "tool_activity_count": 0,
        },
        companion={"busy": True, "last_assistant_text": ""},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.stopped == ["managed"]
    assert len(cdp.messages) == 1
    assert "Riprendi dall'ultimo punto utile" in cdp.messages[0][2]
    assert queue.watchdog_state(job_id)["recovery_count"] == 1
    assert report["actions"][0]["reason"] == "stalled_stream_restarted"


def test_repeated_recovery_uses_external_only_evidence(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(ui={}, companion={})
    runner = shepherd(queue, cdp)

    assert "Riprendi dall'ultimo punto utile" in runner._recovery_prompt(job_id)
    queue.mark_watchdog_recovery(job_id)

    prompt = runner._recovery_prompt(job_id)
    assert "soltanto riferimenti esterni verificabili" in prompt
    assert "Non usare il testo della chat precedente" in prompt


def test_silent_stream_with_only_old_assistant_turn_is_recovered(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 4,
            "assistant_turns": 1,
            "response_in_progress": True,
            "response_pending": True,
            "response_idle_ms": 121_000,
            "progress_idle_ms": 121_000,
            "tool_activity_count": 0,
        },
        companion={"busy": True, "assistant_turns": 1, "last_assistant_text": "Risposta precedente"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.stopped == ["managed"]
    assert len(cdp.messages) == 1
    assert report["actions"][0]["reason"] == "stalled_stream_restarted"


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
        companion={"busy": True, "last_assistant_text": final + "\n[[BOTTAZZI_GOAL_REACHED]]"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    job = queue.get_job(job_id)
    assert job.state is GptJobState.REVIEW
    assert job.last_assistant_text == final
    assert cdp.closed == ["managed"]
    assert report["actions"][0]["reason"] == "goal_complete"

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
        companion={"focused": True, "human_composer_chars": 7, "human_composer_active": True},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert report["actions"][0]["reason"] == "focused_human_draft"


def test_inactive_human_draft_does_not_block_stall_recovery(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 4,
            "assistant_turns": 0,
            "response_in_progress": True,
            "response_pending": True,
            "response_idle_ms": 121_000,
            "progress_idle_ms": 121_000,
            "tool_activity_count": 0,
        },
        companion={"focused": True, "human_composer_chars": 19, "human_composer_active": False, "busy": True, "last_assistant_text": ""},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.stopped == ["managed"]
    assert len(cdp.messages) == 1
    assert queue.watchdog_state(job_id)["recovery_count"] == 1
    assert report["actions"][0]["reason"] == "stalled_stream_restarted"


def test_silent_pending_job_recovers_without_human_wakeup(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": True,
            "response_idle_ms": 121_000,
            "progress_idle_ms": 121_000,
            "tool_activity_count": 0,
        },
        companion={"busy": False, "last_assistant_text": "Risposta precedente"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    job = queue.get_job(job_id)
    assert job.state is GptJobState.ACTIVE
    assert job.target_id == "managed"
    assert len(cdp.messages) == 1
    assert "A che punto sei?" in cdp.messages[0][2]
    assert "Hai risolto?" in cdp.messages[0][2]
    assert queue.get_job(job_id).last_error == "status_probe_pending"
    assert queue.watchdog_state(job_id)["recovery_count"] == 1
    assert report["actions"][0]["reason"] == "unanswered_status_probe"


def test_fresh_silent_pending_job_is_not_recovered_too_early(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": True,
            "response_idle_ms": 119_000,
            "progress_idle_ms": 119_000,
            "tool_activity_count": 0,
        },
        companion={"busy": False, "last_assistant_text": "Risposta precedente"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.messages == []
    assert queue.watchdog_state(job_id)["recovery_count"] == 0
    assert report["actions"][0]["reason"] == "awaiting_settle"


def test_stalled_unanswered_job_is_recovered_before_review(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 181_000,
        }
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    job = queue.get_job(job_id)
    assert job.state is GptJobState.ACTIVE
    assert job.target_id == "managed"
    assert cdp.closed == []
    assert len(cdp.messages) == 1
    assert "A che punto sei?" in cdp.messages[0][2]
    assert "Hai risolto?" in cdp.messages[0][2]
    assert queue.get_job(job_id).last_error == "status_probe_pending"
    assert queue.watchdog_state(job_id)["recovery_count"] == 1
    assert report["actions"][0]["reason"] == "unanswered_status_probe"


def test_composer_consumed_between_probe_and_recovery_spends_no_retry(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)

    class ConsumedCdp(FakeCdp):
        def submit_chatgpt_composer(self, target_id: str, *, wait_timeout_s: float = 8.0):
            return {"submitted": False, "reason": "composer_empty", "composer_chars": 0}

    cdp = ConsumedCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 61_000,
        },
        companion={"composer_chars": 12},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert queue.watchdog_state(job_id)["recovery_count"] == 0
    assert report["actions"][0]["reason"] == "composer_already_consumed"


def test_stale_nonempty_composer_is_resubmitted(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 61_000,
        },
        companion={"composer_chars": 12},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.closed == []
    assert cdp.submitted_composers == ["managed"]
    assert queue.watchdog_state(job_id)["recovery_count"] == 1
    assert report["actions"][0]["reason"] == "composer_resubmitted"



def test_stale_stop_is_woken_by_status_probe_before_hard_restart(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)

    class WakeCdp(FakeCdp):
        def wake_stalled_chatgpt(self, target_id: str, *, text: str = "A che punto sei? Hai risolto?", wait_timeout_s: float = 4.0):
            self.wake_calls.append(target_id)
            return {"submitted": True, "confirmed": True, "text": text}

    cdp = WakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 2,
            "response_in_progress": True,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 181_000,
        },
        companion={"busy": True, "last_assistant_text": "Risposta parziale"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.wake_calls == ["managed"]
    assert cdp.stopped == []
    assert cdp.messages == []
    assert queue.watchdog_state(job_id)["recovery_count"] == 1
    assert queue.get_job(job_id).last_error == "status_probe_pending"
    assert report["actions"][0]["reason"] == "stalled_stream_status_probe"


def test_stale_stream_is_stopped_and_restarted_in_same_chat(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 2,
            "response_in_progress": True,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 181_000,
        },
        companion={"busy": True, "last_assistant_text": "Risposta parziale"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.stopped == ["managed"]
    assert len(cdp.messages) == 1
    assert "Riprendi dall'ultimo punto utile" in cdp.messages[0][2]
    assert queue.watchdog_state(job_id)["recovery_count"] == 1
    assert report["actions"][0]["reason"] == "stalled_stream_restarted"


def test_zombie_conversation_with_zero_turns_recycles_target_without_work_retry(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 0,
            "assistant_turns": 0,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 61_000,
        },
        companion={"busy": False, "composer_chars": 0, "assistant_turns": 0, "last_assistant_text": ""},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.ACTIVE
    assert saved.target_id == "fresh-1"
    assert queue.watchdog_state(job_id)["recovery_count"] == 0
    assert queue.watchdog_state(job_id)["transport_failure_count"] == 1
    assert report["actions"][0]["reason"] == "empty_conversation_target_recycled"


def test_progress_idle_settles_reply_even_when_response_idle_was_reset(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 61_000,
        },
        companion={"busy": False, "last_assistant_text": "Finito.\n[[BOTTAZZI_GOAL_REACHED]]"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.target_id is None
    assert report["actions"][0]["reason"] == "goal_complete"


def test_retry_budget_escalates_to_fresh_target_before_review(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    queue.mark_watchdog_recovery(job_id)
    queue.mark_watchdog_recovery(job_id)
    with queue._connect() as conn:
        conn.execute("UPDATE gpt_job_watchdog SET last_recovery_at=? WHERE job_id=?", (999_000, job_id))
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 181_000,
        }
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.ACTIVE
    assert saved.target_id == "fresh-1"
    assert "managed" in cdp.closed
    assert len(cdp.messages) == 1
    assert cdp.messages[0][0] == "fresh-1"
    assert queue.watchdog_state(job_id)["recovery_count"] == 3
    assert report["actions"][0]["reason"] == "unanswered_fresh_target"


def test_fresh_target_rebind_preserves_project_context_url(tmp_path: Path) -> None:
    queue = GptWorkQueue(tmp_path / "queue.sqlite3", clock=lambda: 1_000_000)
    context_url = "https://chatgpt.com/g/g-p-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-demo/c/job"
    job = queue.create_job(
        "Project managed",
        conversation_url="https://chatgpt.com/c/job",
        conversation_context_url=context_url,
        target_id="managed",
        state=GptJobState.ACTIVE,
    )
    queue.mark_watchdog_recovery(job.job_id)
    queue.mark_watchdog_recovery(job.job_id)
    with queue._connect() as conn:
        conn.execute("UPDATE gpt_job_watchdog SET last_recovery_at=? WHERE job_id=?", (999_000, job.job_id))

    class ProjectCdp(FakeCdp):
        def __init__(self):
            super().__init__(
                ui={
                    "user_turns": 2,
                    "assistant_turns": 1,
                    "response_in_progress": False,
                    "response_pending": False,
                    "response_idle_ms": 0,
                    "progress_idle_ms": 181_000,
                }
            )
            self.navigated = []

        def navigate_chatgpt_conversation(self, target_id: str, url: str):
            self.navigated.append((target_id, url))
            return super().navigate_chatgpt_conversation(target_id, url)

    cdp = ProjectCdp()
    report = shepherd(queue, cdp).run_once(auto_start=False)

    saved = queue.get_job(job.job_id)
    assert saved.state is GptJobState.ACTIVE
    assert saved.target_id == "fresh-1"
    assert cdp.navigated[-1] == ("fresh-1", context_url)
    assert cdp.messages[-1][1] == context_url
    assert report["actions"][0]["reason"] == "unanswered_fresh_target"


def test_exhausted_watchdog_releases_instead_of_arenating_forever(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    queue.mark_watchdog_recovery(job_id)
    queue.mark_watchdog_recovery(job_id)
    queue.mark_watchdog_recovery(job_id)
    with queue._connect() as conn:
        conn.execute("UPDATE gpt_job_watchdog SET last_recovery_at=? WHERE job_id=?", (999_000, job_id))
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 0,
            "progress_idle_ms": 181_000,
        }
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.target_id is None
    assert saved.last_error == "worker_stalled_after_retries"
    assert report["actions"][0]["reason"] == "unanswered_stalled_after_retries"


def test_transport_timeouts_do_not_consume_recovery_budget_and_recycle_target(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)

    class TransportFailCdp(FakeCdp):
        def chatgpt_ui_state(self, target_id: str):
            raise CdpError("cdp_transport_error:Runtime.evaluate:WebSocketTimeoutException")

    cdp = TransportFailCdp(ui={})
    runner = shepherd(queue, cdp)

    first = runner.run_once(auto_start=False)
    second = runner.run_once(auto_start=False)
    third = runner.run_once(auto_start=False)

    assert first["actions"][0]["reason"].startswith("transport_retry:probe:")
    assert second["actions"][0]["reason"].startswith("transport_retry:probe:")
    assert third["actions"][0]["reason"] == "transport_target_recycled"
    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.ACTIVE
    assert saved.target_id == "fresh-1"
    state = queue.watchdog_state(job_id)
    assert state["recovery_count"] == 0
    assert state["transport_failure_count"] == 0


def test_queue_busy_is_existing_delivery_not_failed_recovery(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)

    class QueueBusyCdp(FakeCdp):
        def queue_human_message(self, target_id: str, conversation_url: str, text: str):
            return {"queued": False, "reason": "queue_busy"}

    cdp = QueueBusyCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 61_000,
            "progress_idle_ms": 61_000,
        },
        companion={"busy": False, "last_assistant_text": "Fase conclusa, resta altro da fare.\n[[BOTTAZZI_GOAL_CONTINUE]]"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert queue.watchdog_state(job_id)["recovery_count"] == 0
    assert report["actions"][0]["reason"] == "delivery_already_queued"


def test_temporary_access_limit_holds_queue_and_sends_nothing(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 2,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": True,
            "response_idle_ms": 999_999,
            "progress_idle_ms": 999_999,
            "temporary_access_limited": True,
        },
        companion={"busy": False, "last_assistant_text": "Risposta precedente"},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert cdp.holds == [("managed", True)]
    assert cdp.messages == []
    assert cdp.wake_calls == []
    assert cdp.closed == []
    assert report["actions"][0]["reason"] == "temporary_access_limited_backoff"


def test_status_probe_reply_uses_short_settle_window(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    queue.set_last_assistant_text(job_id, "Vecchio stato")
    queue.set_status_probe_baseline(
        job_id,
        assistant_turns=1,
        user_turns=2,
        assistant_text="Vecchio stato",
    )
    queue.set_state(job_id, GptJobState.ACTIVE, last_error="status_probe_pending")
    cdp = FakeCdp(
        ui={"user_turns": 2, "assistant_turns": 2, "response_in_progress": False, "response_pending": False, "response_idle_ms": 16_000, "progress_idle_ms": 16_000},
        companion={"busy": False, "last_assistant_text": "Sono fermo al passo X; resta da completare Y."},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).last_error is None
    assert len(cdp.messages) == 1
    assert "Continua automaticamente dal punto raggiunto" in cdp.messages[0][2]
    assert report["actions"][0]["reason"] == "status_probe_resumed"


def test_status_probe_reply_does_not_continue_before_short_settle(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    queue.set_last_assistant_text(job_id, "Vecchio stato")
    queue.set_status_probe_baseline(
        job_id,
        assistant_turns=1,
        user_turns=2,
        assistant_text="Vecchio stato",
    )
    queue.set_state(job_id, GptJobState.ACTIVE, last_error="status_probe_pending")
    cdp = FakeCdp(
        ui={"user_turns": 2, "assistant_turns": 2, "response_in_progress": False, "response_pending": False, "response_idle_ms": 14_000, "progress_idle_ms": 14_000},
        companion={"busy": False, "last_assistant_text": "Sono fermo al passo X; resta da completare Y."},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    assert queue.get_job(job_id).last_error == "status_probe_pending"
    assert cdp.messages == []
    assert report["actions"][0]["reason"] == "awaiting_settle"


def test_status_probe_reply_resumes_goal_in_same_chat(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    queue.set_last_assistant_text(job_id, "Vecchio stato")
    queue.set_status_probe_baseline(
        job_id,
        assistant_turns=1,
        user_turns=2,
        assistant_text="Vecchio stato",
    )
    queue.set_state(job_id, GptJobState.ACTIVE, last_error="status_probe_pending")
    cdp = FakeCdp(
        ui={"user_turns": 2, "assistant_turns": 2, "response_in_progress": False, "response_pending": False, "response_idle_ms": 61_000, "progress_idle_ms": 61_000},
        companion={"busy": False, "last_assistant_text": "Sono fermo al passo X; resta da completare Y."},
    )
    report = shepherd(queue, cdp).run_once(auto_start=False)
    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.ACTIVE
    assert saved.last_error is None
    assert len(cdp.messages) == 1
    assert "Continua automaticamente dal punto raggiunto" in cdp.messages[0][2]
    assert queue.status_probe_state(job_id)["sent_at"] == 0
    assert report["actions"][0]["reason"] == "status_probe_resumed"


def test_status_probe_new_text_beats_stale_stop_and_turn_mismatch(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    old_text = "Sto ancora lavorando al passo precedente."
    queue.set_last_assistant_text(job_id, old_text)
    queue.set_status_probe_baseline(
        job_id,
        assistant_turns=2,
        user_turns=5,
        assistant_text=old_text,
    )
    queue.set_state(job_id, GptJobState.ACTIVE, last_error="status_probe_pending")
    cdp = FakeCdp(
        ui={"user_turns": 6, "assistant_turns": 2, "response_in_progress": True, "response_pending": True, "response_idle_ms": 61_000, "progress_idle_ms": 61_000, "tool_activity_count": 0},
        companion={"busy": True, "last_assistant_text": "Il probe ha risposto: resta da completare il passo finale."},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.ACTIVE
    assert saved.last_error is None
    assert cdp.stopped == ["managed"]
    assert len(cdp.messages) == 1
    assert "Continua automaticamente dal punto raggiunto" in cdp.messages[0][2]
    assert queue.status_probe_state(job_id)["sent_at"] == 0
    assert report["actions"][0]["reason"] == "status_probe_resumed"


def test_status_probe_unchanged_text_does_not_false_resume_stale_stop(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    old_text = "Sto ancora lavorando al passo precedente."
    queue.set_last_assistant_text(job_id, old_text)
    queue.set_status_probe_baseline(
        job_id,
        assistant_turns=2,
        user_turns=5,
        assistant_text=old_text,
    )
    queue.set_state(job_id, GptJobState.ACTIVE, last_error="status_probe_pending")
    cdp = FakeCdp(
        ui={"user_turns": 6, "assistant_turns": 2, "response_in_progress": True, "response_pending": True, "response_idle_ms": 61_000, "progress_idle_ms": 61_000, "tool_activity_count": 0},
        companion={"busy": True, "last_assistant_text": old_text},
    )

    report = shepherd(queue, cdp).run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.ACTIVE
    assert saved.last_error == "status_probe_pending"
    assert cdp.stopped == []
    assert cdp.messages == []
    assert queue.status_probe_state(job_id)["sent_at"] > 0
    assert report["actions"][0]["reason"] == "response_in_progress_silent"


def test_goal_reply_without_status_marker_stops_in_review_instead_of_looping(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    with queue._connect() as conn:
        conn.execute("UPDATE gpt_jobs SET prompt=? WHERE job_id=?", ("Fai il lavoro.\\n\\nBOT-TAZZI GOAL LOOP", job_id))
    class GoalCdp(FakeCdp):
        def __init__(self):
            super().__init__(ui={"user_turns":1,"assistant_turns":1,"response_in_progress":False,"response_pending":False,"response_idle_ms":61000}, companion={"busy":False,"last_assistant_text":"Ho completato una fase, ma resta altro da fare."})
            self.messages=[]
        def install_human_input_target(self,target_id,conversation_url): return {"ok":True}
        def queue_human_message(self,target_id,conversation_url,text): self.messages.append((target_id,conversation_url,text)); return {"queued":True}
    notices=[]; cdp=GoalCdp()
    runner=GptQueueShepherd(queue,cdp,policy=GptQueueShepherdPolicy(complete_idle_ms=60000,stalled_idle_ms=180000),completion_notifier=lambda title:notices.append(title) or {"ok":True})
    report=runner.run_once(auto_start=False)
    saved=queue.get_job(job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.last_error == "goal_status_missing"
    assert cdp.messages == []
    assert notices==[]
    assert report["actions"][0]["reason"]=="goal_status_missing"


def test_goal_continue_marker_auto_continues_same_chat_without_notification(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    class GoalCdp(FakeCdp):
        def __init__(self):
            super().__init__(ui={"user_turns":1,"assistant_turns":1,"response_in_progress":False,"response_pending":False,"response_idle_ms":61000}, companion={"busy":False,"last_assistant_text":"Ho completato una fase.\\n[[BOTTAZZI_GOAL_CONTINUE]]"})
            self.messages=[]
        def install_human_input_target(self,target_id,conversation_url): return {"ok":True}
        def queue_human_message(self,target_id,conversation_url,text): self.messages.append((target_id,conversation_url,text)); return {"queued":True}
    notices=[]; cdp=GoalCdp()
    runner=GptQueueShepherd(queue,cdp,policy=GptQueueShepherdPolicy(complete_idle_ms=60000,stalled_idle_ms=180000),completion_notifier=lambda title:notices.append(title) or {"ok":True})
    report=runner.run_once(auto_start=False)
    assert queue.get_job(job_id).state is GptJobState.ACTIVE
    assert len(cdp.messages)==1
    assert "[[BOTTAZZI_GOAL_CONTINUE]]" in cdp.messages[0][2]
    assert notices==[]
    assert report["actions"][0]["reason"]=="goal_continue_marker"


def test_goal_continue_marker_is_sent_once_until_assistant_turn_changes(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    marker_text = "Ho completato una fase.\n[[BOTTAZZI_GOAL_CONTINUE]]"
    first_cdp = FakeCdp(
        ui={"user_turns": 1, "assistant_turns": 1, "response_in_progress": False, "response_pending": False, "response_idle_ms": 61_000},
        companion={"busy": False, "last_assistant_text": marker_text},
    )
    first = shepherd(queue, first_cdp).run_once(auto_start=False)
    assert len(first_cdp.messages) == 1
    assert first["actions"][0]["reason"] == "goal_continue_marker"

    reopened = GptWorkQueue(queue.path, clock=lambda: 1_000_015)
    second_cdp = FakeCdp(
        ui={"user_turns": 2, "assistant_turns": 1, "response_in_progress": False, "response_pending": False, "response_idle_ms": 76_000},
        companion={"busy": False, "last_assistant_text": marker_text},
    )
    second = shepherd(reopened, second_cdp).run_once(auto_start=False)
    assert second_cdp.messages == []
    assert second["actions"][0]["reason"] == "goal_continue_already_sent"

    next_text = "Ho completato anche la fase successiva.\n[[BOTTAZZI_GOAL_CONTINUE]]"
    third_cdp = FakeCdp(
        ui={"user_turns": 2, "assistant_turns": 2, "response_in_progress": False, "response_pending": False, "response_idle_ms": 61_000},
        companion={"busy": False, "last_assistant_text": next_text},
    )
    third = shepherd(reopened, third_cdp).run_once(auto_start=False)
    assert len(third_cdp.messages) == 1
    assert third["actions"][0]["reason"] == "goal_continue_marker"


def test_goal_marker_releases_and_notifies(tmp_path: Path) -> None:
    queue,job_id=queue_with_active(tmp_path)
    with queue._connect() as conn:
        conn.execute("UPDATE gpt_jobs SET prompt=? WHERE job_id=?", ("Fai il lavoro.\\n\\nBOT-TAZZI GOAL LOOP", job_id))
    cdp=FakeCdp(ui={"user_turns":1,"assistant_turns":1,"response_in_progress":False,"response_pending":False,"response_idle_ms":61000}, companion={"busy":False,"last_assistant_text":"Lavoro completato.\\n[[BOTTAZZI_GOAL_REACHED]]"})
    notices=[]
    runner=GptQueueShepherd(queue,cdp,policy=GptQueueShepherdPolicy(complete_idle_ms=60000,stalled_idle_ms=180000),completion_notifier=lambda title:notices.append(title) or {"ok":True})
    report=runner.run_once(auto_start=False)
    saved=queue.get_job(job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.target_id is None
    assert "[[BOTTAZZI_GOAL_REACHED]]" not in saved.last_assistant_text
    assert notices==["Managed"]
    assert report["actions"][0]["reason"]=="goal_complete"


def test_goal_blocked_marker_releases_without_completion_notification(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    with queue._connect() as conn:
        conn.execute("UPDATE gpt_jobs SET prompt=? WHERE job_id=?", ("Fai il lavoro.\n\nBOT-TAZZI GOAL LOOP", job_id))
    cdp = FakeCdp(
        ui={
            "user_turns": 1,
            "assistant_turns": 1,
            "response_in_progress": False,
            "response_pending": False,
            "response_idle_ms": 61_000,
        },
        companion={
            "busy": False,
            "last_assistant_text": "Mi serve un permesso umano indispensabile.\n[[BOTTAZZI_GOAL_BLOCKED]]",
        },
    )
    notices = []
    runner = GptQueueShepherd(
        queue,
        cdp,
        policy=GptQueueShepherdPolicy(complete_idle_ms=60_000, stalled_idle_ms=180_000),
        completion_notifier=lambda title: notices.append(title) or {"ok": True},
    )

    report = runner.run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.target_id is None
    assert saved.last_error == "goal_blocked"
    assert "[[BOTTAZZI_GOAL_BLOCKED]]" not in saved.last_assistant_text
    assert notices == []
    assert report["actions"][0]["reason"] == "goal_blocked"


def test_goal_marker_wins_over_stale_stop_button_and_turn_count_mismatch(tmp_path: Path) -> None:
    queue, job_id = queue_with_active(tmp_path)
    cdp = FakeCdp(
        ui={
            "user_turns": 6,
            "assistant_turns": 2,
            "response_in_progress": True,
            "response_pending": True,
            "response_idle_ms": 61_000,
            "progress_idle_ms": 61_000,
        },
        companion={
            "busy": True,
            "assistant_turns": 2,
            "last_assistant_text": "Il GOAL è verificato.\n[[BOTTAZZI_GOAL_REACHED]]",
        },
    )
    notices = []
    runner = GptQueueShepherd(
        queue,
        cdp,
        policy=GptQueueShepherdPolicy(complete_idle_ms=60_000, stalled_idle_ms=180_000),
        completion_notifier=lambda title: notices.append(title) or {"ok": True},
    )

    report = runner.run_once(auto_start=False)

    saved = queue.get_job(job_id)
    assert saved.state is GptJobState.REVIEW
    assert saved.target_id is None
    assert cdp.stopped == ["managed"]
    assert cdp.messages == []
    assert notices == ["Managed"]
    assert report["actions"][0]["reason"] == "goal_complete"


def test_default_completion_notifier_uses_telemetry_prefix(monkeypatch):
    sent = {}

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def connect(self, path):
            sent["path"] = path

        def send(self, payload):
            sent["payload"] = payload
            return len(payload)

    monkeypatch.setattr("ralfloop_agent.integration.gpt_queue_shepherd.socket.socket", lambda *args, **kwargs: FakeSocket())
    result = GptQueueShepherd._notify_completion("Calendario")
    assert result == {"ok": True, "message": "BOT-TAZZI · ✅ Calendario — completato"}
    assert sent["payload"] == "BOT-TAZZI · ✅ Calendario — completato".encode("utf-8")
