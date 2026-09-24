from __future__ import annotations

from dataclasses import dataclass
import os
import socket
from typing import Any, Callable

from .gpt_browser_cdp import ChromeCdp, CdpError
from .gpt_frontend import GptWorkController
from .gpt_work_queue import GptJobState, GptWorkQueue

GOAL_MARKER = "[[BOTTAZZI_GOAL_REACHED]]"
GOAL_BLOCKED_MARKER = "[[BOTTAZZI_GOAL_BLOCKED]]"
GOAL_CONTINUATION = (
    "Continua automaticamente il lavoro verso il GOAL definito nel messaggio iniziale. "
    "Non chiedere conferme e non ripetere quanto già completato. "
    "Verifica concretamente i criteri di accettazione prima di dichiarare il GOAL raggiunto; una fase, un piano o un risultato parziale non bastano. "
    "Se sei realmente bloccato da un dato, permesso o intervento umano indispensabile, spiega cosa manca e termina con [[BOTTAZZI_GOAL_BLOCKED]]. "
    "Quando e solo quando il GOAL è davvero raggiunto, termina con una riga contenente esattamente [[BOTTAZZI_GOAL_REACHED]]."
)


@dataclass(frozen=True)
class GptQueueShepherdPolicy:
    complete_idle_ms: int = 60_000
    stalled_idle_ms: int = 180_000

    def __post_init__(self) -> None:
        if self.complete_idle_ms < 5_000:
            raise ValueError("complete_idle_ms_too_small")
        if self.stalled_idle_ms < self.complete_idle_ms:
            raise ValueError("stalled_idle_ms_before_complete_idle_ms")


class GptQueueShepherd:
    """Queue-aware lifecycle maintenance for locally managed ChatGPT tabs.

    The durable queue remains authoritative. Only ACTIVE jobs bound to their exact
    conversation target are considered. Unmanaged ChatGPT tabs are never closed.
    """

    def __init__(
        self,
        queue: GptWorkQueue,
        cdp: ChromeCdp,
        *,
        policy: GptQueueShepherdPolicy | None = None,
        completion_notifier: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.queue = queue
        self.cdp = cdp
        self.controller = GptWorkController(queue, cdp)
        self.policy = policy or GptQueueShepherdPolicy()
        self.completion_notifier = completion_notifier or self._notify_completion

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _substantive_response_text(value: Any) -> str:
        text = str(value or "").strip()
        normalized = " ".join(text.casefold().split()).strip(" .…")
        if normalized in {
            "sto pensando",
            "thinking",
            "sto cercando",
            "searching",
            "working",
            "elaborazione in corso",
            "ricerca in corso",
        }:
            return ""
        return text

    @staticmethod
    def _notify_completion(title: str) -> dict[str, Any]:
        socket_path = os.getenv("BOTTAZZI_TELEMETRY_SOCKET", "/run/bottazzi-telemetry.sock")
        clean_title = " ".join(str(title or "Lavoro GPT").split())[:240]
        message = f"BOT-TAZZI · lavoro completato: {clean_title}"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as client:
                client.connect(socket_path)
                client.send(message.encode("utf-8"))
        except OSError as exc:
            return {"ok": False, "error": type(exc).__name__}
        return {"ok": True, "message": message}

    def run_once(self, *, auto_start: bool = True) -> dict[str, Any]:
        self.controller.reconcile()
        actions: list[dict[str, Any]] = []

        for job in list(self.queue.list_jobs()):
            if job.state is not GptJobState.ACTIVE or not job.target_id or not job.conversation_url:
                continue

            try:
                # Fail closed if the target was rebound or reused for another chat.
                self.controller._exact_job_target(job)
                ui = self.cdp.chatgpt_ui_state(job.target_id)
                companion = self.cdp.chatgpt_companion_state(job.target_id)
            except (CdpError, OSError, RuntimeError, ValueError) as exc:
                actions.append(
                    {
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": f"probe_failed:{str(exc)[:200]}",
                    }
                )
                continue

            focused = bool(companion.get("focused"))
            companion_busy = bool(companion.get("busy"))
            composer_chars = self._int(companion.get("composer_chars"))
            response_text = self._substantive_response_text(companion.get("last_assistant_text"))
            response_in_progress = bool(ui.get("response_in_progress"))
            response_pending = bool(ui.get("response_pending"))
            if response_text and (response_in_progress or response_pending):
                self.queue.set_last_assistant_text(job.job_id, response_text)
            if focused:
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "focused"})
                continue
            if composer_chars:
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "composer_not_empty"})
                continue
            if response_in_progress:
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "response_in_progress"})
                continue

            user_turns = self._int(ui.get("user_turns"))
            assistant_turns = self._int(ui.get("assistant_turns"))
            pending = bool(ui.get("response_pending"))
            idle_ms = self._int(ui.get("response_idle_ms"))
            answered = user_turns > 0 and assistant_turns >= user_turns
            saved_text = self._substantive_response_text(
                self.queue.get_job(job.job_id).last_assistant_text
            )
            final_text = response_text or saved_text

            if companion_busy and idle_ms < self.policy.complete_idle_ms:
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "companion_busy"})
                continue

            completed = (
                answered
                and bool(final_text)
                and idle_ms >= self.policy.complete_idle_ms
            )
            stalled = (
                pending
                and idle_ms >= self.policy.stalled_idle_ms
                and (user_turns > assistant_turns or not final_text)
            )

            if completed:
                # Every ACTIVE queue job is GOAL-managed, including imported legacy chats.
                goal_managed = True
                goal_reached = GOAL_MARKER in final_text
                goal_blocked = GOAL_BLOCKED_MARKER in final_text
                clean_final_text = (
                    final_text.replace(GOAL_MARKER, "").replace(GOAL_BLOCKED_MARKER, "").strip()
                )
                clean_saved_text = (
                    saved_text.replace(GOAL_MARKER, "").replace(GOAL_BLOCKED_MARKER, "").strip()
                )
                persisted_text = clean_final_text
                if clean_saved_text:
                    minimum_final_chars = max(120, len(clean_saved_text) // 2)
                    if not clean_final_text or len(clean_final_text) < minimum_final_chars:
                        persisted_text = clean_saved_text
                if persisted_text:
                    self.queue.set_last_assistant_text(job.job_id, persisted_text)
                if goal_blocked:
                    self.controller.release_job(job.job_id)
                    self.queue.set_state(job.job_id, GptJobState.REVIEW, last_error="goal_blocked")
                    actions.append(
                        {
                            "job_id": job.job_id,
                            "action": "released",
                            "reason": "goal_blocked",
                            "user_turns": user_turns,
                            "assistant_turns": assistant_turns,
                            "response_idle_ms": idle_ms,
                        }
                    )
                    continue
                if not goal_reached:
                    try:
                        continuation = self.controller.send_message(job.job_id, GOAL_CONTINUATION)
                        actions.append(
                            {
                                "job_id": job.job_id,
                                "action": "continued",
                                "reason": "goal_not_reached",
                                "user_turns": user_turns,
                                "assistant_turns": assistant_turns,
                                "response_idle_ms": idle_ms,
                                "continuation": continuation,
                            }
                        )
                    except (CdpError, OSError, RuntimeError, ValueError) as exc:
                        actions.append(
                            {
                                "job_id": job.job_id,
                                "action": "preserved",
                                "reason": f"goal_continue_failed:{str(exc)[:200]}",
                            }
                        )
                    continue
                notification = self.completion_notifier(job.title)
                self.controller.release_job(job.job_id)
                actions.append(
                    {
                        "job_id": job.job_id,
                        "action": "released",
                        "reason": "goal_complete",
                        "user_turns": user_turns,
                        "assistant_turns": assistant_turns,
                        "response_idle_ms": idle_ms,
                        "telegram_notification": notification,
                    }
                )
                continue

            if stalled:
                self.controller.release_job(job.job_id)
                self.queue.set_state(
                    job.job_id,
                    GptJobState.REVIEW,
                    last_error="worker_stalled_without_reply",
                )
                actions.append(
                    {
                        "job_id": job.job_id,
                        "action": "released",
                        "reason": "worker_stalled_without_reply",
                        "user_turns": user_turns,
                        "assistant_turns": assistant_turns,
                        "response_idle_ms": idle_ms,
                    }
                )
                continue

            actions.append(
                {
                    "job_id": job.job_id,
                    "action": "preserved",
                    "reason": "awaiting_settle",
                    "user_turns": user_turns,
                    "assistant_turns": assistant_turns,
                    "response_pending": pending,
                    "response_idle_ms": idle_ms,
                }
            )

        pump: dict[str, Any] | None = None
        if auto_start:
            pump = self.controller.pump()

        return {
            "ok": True,
            "actions": actions,
            "pump": pump,
            "runtime": self.controller.runtime_state(),
        }
