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
STALL_RECOVERY = (
    "Riprendi automaticamente dall'ultimo messaggio utente rimasto senza una risposta completa. "
    "Non ripartire da zero e non chiedere conferme. Continua il lavoro verso il GOAL già definito, "
    "verifica lo stato reale e porta a termine ciò che manca."
)


@dataclass(frozen=True)
class GptQueueShepherdPolicy:
    complete_idle_ms: int = 60_000
    stalled_idle_ms: int = 180_000
    recovery_cooldown_ms: int = 90_000
    max_recoveries: int = 2

    def __post_init__(self) -> None:
        if self.complete_idle_ms < 5_000:
            raise ValueError("complete_idle_ms_too_small")
        if self.stalled_idle_ms < self.complete_idle_ms:
            raise ValueError("stalled_idle_ms_before_complete_idle_ms")
        if self.recovery_cooldown_ms < 5_000:
            raise ValueError("recovery_cooldown_ms_too_small")
        if self.max_recoveries < 1:
            raise ValueError("max_recoveries_too_small")


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

    def _idle_ms(self, ui: dict[str, Any]) -> int:
        return max(
            self._int(ui.get("response_idle_ms")),
            self._int(ui.get("progress_idle_ms")),
        )

    @staticmethod
    def _is_transport_error(exc: Exception) -> bool:
        text = str(exc)
        return any(
            token in text
            for token in (
                "cdp_timeout:",
                "cdp_transport_error:",
                "cdp_unavailable:",
                "WebSocketTimeoutException",
            )
        )

    @staticmethod
    def _is_queue_busy(exc: Exception) -> bool:
        return str(exc).strip() == "queue_busy"

    def _transport_failure(
        self,
        job: Any,
        actions: list[dict[str, Any]],
        *,
        phase: str,
        exc: Exception,
    ) -> None:
        watchdog = self.queue.mark_watchdog_transport_failure(job.job_id)
        if watchdog["transport_failure_count"] < 3:
            actions.append(
                {
                    "job_id": job.job_id,
                    "action": "preserved",
                    "reason": f"transport_retry:{phase}:{str(exc)[:120]}",
                    "watchdog": watchdog,
                }
            )
            return
        try:
            recycled = self.controller.recycle_job_target(job.job_id)
            self.queue.reset_watchdog_transport_failures(job.job_id)
            actions.append(
                {
                    "job_id": job.job_id,
                    "action": "recovered",
                    "reason": "transport_target_recycled",
                    "phase": phase,
                    "watchdog": self.queue.watchdog_state(job.job_id),
                    "recovery": recycled,
                }
            )
        except (CdpError, OSError, RuntimeError, ValueError) as recycle_exc:
            actions.append(
                {
                    "job_id": job.job_id,
                    "action": "preserved",
                    "reason": f"transport_recycle_failed:{str(recycle_exc)[:160]}",
                    "watchdog": watchdog,
                }
            )

    def _recovery_gate(self, job_id: str) -> tuple[str, dict[str, int]]:
        state = self.queue.watchdog_state(job_id)
        last = state["last_recovery_at"]
        if last:
            elapsed_ms = max(0, int(self.queue.clock()) - last) * 1000
            if elapsed_ms < self.policy.recovery_cooldown_ms:
                return "cooldown", state
        if state["recovery_count"] < self.policy.max_recoveries:
            return "ready", state
        if state["recovery_count"] == self.policy.max_recoveries:
            return "rebind", state
        return "exhausted", state

    def _rebind_stalled(self, job: Any, actions: list[dict[str, Any]], *, reason: str, idle_ms: int) -> bool:
        context_url = job.conversation_context_url or job.conversation_url
        if not context_url or not job.target_id:
            return False
        old_target_id = job.target_id
        new_target_id: str | None = None
        rebound = False
        try:
            new_target_id = self.cdp.create_chatgpt_target(clear_cache=False, background=True)
            self.cdp.navigate_chatgpt_conversation(new_target_id, context_url)
            self.cdp.install_human_input_target(new_target_id, context_url)
            self.cdp.close_target(old_target_id)
            self.queue.bind_chat(
                job.job_id,
                conversation_url=job.conversation_url,
                conversation_context_url=context_url,
                target_id=new_target_id,
                state=GptJobState.ACTIVE,
                last_error=None,
            )
            rebound = True
            recovery = self.controller.send_message(job.job_id, STALL_RECOVERY)
            watchdog = self.queue.mark_watchdog_recovery(job.job_id)
            actions.append(
                {
                    "job_id": job.job_id,
                    "action": "recovered",
                    "reason": reason,
                    "progress_idle_ms": idle_ms,
                    "old_target_id": old_target_id,
                    "new_target_id": new_target_id,
                    "watchdog": watchdog,
                    "recovery": recovery,
                }
            )
            return True
        except (CdpError, OSError, RuntimeError, ValueError) as exc:
            if new_target_id and not rebound:
                try:
                    self.cdp.close_target(new_target_id)
                except (CdpError, OSError, RuntimeError, ValueError):
                    pass
            if self._is_queue_busy(exc):
                actions.append(
                    {
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "delivery_already_queued",
                        "progress_idle_ms": idle_ms,
                        "watchdog": self.queue.watchdog_state(job.job_id),
                    }
                )
            elif self._is_transport_error(exc):
                current = self.queue.get_job(job.job_id)
                self._transport_failure(current, actions, phase="fresh_target_rebind", exc=exc)
            else:
                actions.append(
                    {
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": f"fresh_target_rebind_failed:{str(exc)[:160]}",
                        "progress_idle_ms": idle_ms,
                    }
                )
            return False

    def _release_stalled(self, job_id: str, actions: list[dict[str, Any]], *, reason: str, idle_ms: int) -> None:
        self.controller.release_job(job_id)
        self.queue.set_state(job_id, GptJobState.REVIEW, last_error="worker_stalled_after_retries")
        actions.append(
            {
                "job_id": job_id,
                "action": "released",
                "reason": reason,
                "progress_idle_ms": idle_ms,
                "watchdog": self.queue.watchdog_state(job_id),
            }
        )

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
                if self._is_transport_error(exc):
                    self._transport_failure(job, actions, phase="probe", exc=exc)
                else:
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
            pending = bool(ui.get("response_pending"))
            user_turns = self._int(ui.get("user_turns"))
            assistant_turns = max(
                self._int(ui.get("assistant_turns")),
                self._int(companion.get("assistant_turns")),
            )
            idle_ms = self._idle_ms(ui)
            current_job = self.queue.get_job(job.job_id)
            saved_text = self._substantive_response_text(current_job.last_assistant_text)
            if response_text and response_text != saved_text:
                self.queue.reset_watchdog(job.job_id)
                self.queue.reset_watchdog_transport_failures(job.job_id)
                if response_in_progress or pending:
                    self.queue.set_last_assistant_text(job.job_id, response_text)
                    saved_text = response_text
            elif idle_ms < self.policy.complete_idle_ms:
                self.queue.reset_watchdog_transport_failures(job.job_id)
            final_text = response_text or saved_text
            answered = user_turns > 0 and assistant_turns >= user_turns

            if focused:
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "focused"})
                continue

            if composer_chars:
                if idle_ms < self.policy.complete_idle_ms:
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "composer_not_empty_fresh",
                        "progress_idle_ms": idle_ms,
                    })
                    continue
                gate, watchdog = self._recovery_gate(job.job_id)
                if gate == "exhausted":
                    self._release_stalled(job.job_id, actions, reason="composer_stalled_after_retries", idle_ms=idle_ms)
                    continue
                if gate == "cooldown":
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "recovery_cooldown",
                        "progress_idle_ms": idle_ms,
                        "watchdog": watchdog,
                    })
                    continue
                try:
                    submitted = self.cdp.submit_chatgpt_composer(job.target_id)
                    if not bool(submitted.get("submitted")):
                        reason = str(submitted.get("reason") or "composer_submit_failed")
                        if reason == "composer_empty":
                            actions.append({
                                "job_id": job.job_id,
                                "action": "preserved",
                                "reason": "composer_already_consumed",
                                "progress_idle_ms": idle_ms,
                                "watchdog": self.queue.watchdog_state(job.job_id),
                            })
                            continue
                        raise CdpError(reason)
                    watchdog = self.queue.mark_watchdog_recovery(job.job_id)
                    actions.append({
                        "job_id": job.job_id,
                        "action": "recovered",
                        "reason": "composer_resubmitted",
                        "progress_idle_ms": idle_ms,
                        "watchdog": watchdog,
                        "recovery": submitted,
                    })
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    if self._is_queue_busy(exc):
                        actions.append({
                            "job_id": job.job_id,
                            "action": "preserved",
                            "reason": "delivery_already_queued",
                            "watchdog": watchdog,
                        })
                    elif self._is_transport_error(exc):
                        self._transport_failure(job, actions, phase="composer_submit", exc=exc)
                    else:
                        actions.append({
                            "job_id": job.job_id,
                            "action": "preserved",
                            "reason": f"composer_recovery_failed:{str(exc)[:160]}",
                            "watchdog": watchdog,
                        })
                continue

            if response_in_progress:
                if idle_ms < self.policy.stalled_idle_ms:
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "response_in_progress",
                        "progress_idle_ms": idle_ms,
                    })
                    continue
                gate, watchdog = self._recovery_gate(job.job_id)
                if gate == "exhausted":
                    self._release_stalled(job.job_id, actions, reason="stream_stalled_after_retries", idle_ms=idle_ms)
                    continue
                if gate == "cooldown":
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "recovery_cooldown",
                        "progress_idle_ms": idle_ms,
                        "watchdog": watchdog,
                    })
                    continue
                if gate == "rebind":
                    self._rebind_stalled(job, actions, reason="stalled_stream_fresh_target", idle_ms=idle_ms)
                    continue
                try:
                    stopped = self.cdp.stop_chatgpt_response(job.target_id)
                    partial = self._substantive_response_text(stopped.get("last_assistant_text"))
                    if partial:
                        self.queue.set_last_assistant_text(job.job_id, partial)
                    continuation = self.controller.send_message(job.job_id, GOAL_CONTINUATION)
                    watchdog = self.queue.mark_watchdog_recovery(job.job_id)
                    actions.append({
                        "job_id": job.job_id,
                        "action": "recovered",
                        "reason": "stalled_stream_restarted",
                        "progress_idle_ms": idle_ms,
                        "watchdog": watchdog,
                        "stopped": stopped,
                        "continuation": continuation,
                    })
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    if self._is_queue_busy(exc):
                        actions.append({
                            "job_id": job.job_id,
                            "action": "preserved",
                            "reason": "delivery_already_queued",
                            "watchdog": watchdog,
                        })
                    elif self._is_transport_error(exc):
                        self._transport_failure(job, actions, phase="stream_recovery", exc=exc)
                    else:
                        actions.append({
                            "job_id": job.job_id,
                            "action": "preserved",
                            "reason": f"stream_recovery_failed:{str(exc)[:160]}",
                            "watchdog": watchdog,
                        })
                continue

            if companion_busy and idle_ms < self.policy.complete_idle_ms:
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "companion_busy"})
                continue

            completed = (
                answered
                and bool(final_text)
                and idle_ms >= self.policy.complete_idle_ms
            )
            stalled = (
                idle_ms >= self.policy.stalled_idle_ms
                and (pending or user_turns > assistant_turns or not final_text)
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
                        self.queue.reset_watchdog(job.job_id)
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
                        if self._is_queue_busy(exc):
                            actions.append(
                                {
                                    "job_id": job.job_id,
                                    "action": "preserved",
                                    "reason": "delivery_already_queued",
                                }
                            )
                        elif self._is_transport_error(exc):
                            self._transport_failure(job, actions, phase="goal_continue", exc=exc)
                        else:
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
                gate, watchdog = self._recovery_gate(job.job_id)
                if gate == "exhausted":
                    self._release_stalled(job.job_id, actions, reason="unanswered_stalled_after_retries", idle_ms=idle_ms)
                    continue
                if gate == "cooldown":
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "recovery_cooldown",
                        "progress_idle_ms": idle_ms,
                        "watchdog": watchdog,
                    })
                    continue
                if gate == "rebind":
                    self._rebind_stalled(job, actions, reason="unanswered_fresh_target", idle_ms=idle_ms)
                    continue
                try:
                    recovery = self.controller.send_message(job.job_id, STALL_RECOVERY)
                    watchdog = self.queue.mark_watchdog_recovery(job.job_id)
                    actions.append(
                        {
                            "job_id": job.job_id,
                            "action": "recovered",
                            "reason": "unanswered_restarted",
                            "user_turns": user_turns,
                            "assistant_turns": assistant_turns,
                            "progress_idle_ms": idle_ms,
                            "watchdog": watchdog,
                            "recovery": recovery,
                        }
                    )
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    if self._is_queue_busy(exc):
                        actions.append(
                            {
                                "job_id": job.job_id,
                                "action": "preserved",
                                "reason": "delivery_already_queued",
                                "watchdog": watchdog,
                            }
                        )
                    elif self._is_transport_error(exc):
                        self._transport_failure(job, actions, phase="unanswered_recovery", exc=exc)
                    else:
                        actions.append(
                            {
                                "job_id": job.job_id,
                                "action": "preserved",
                                "reason": f"unanswered_recovery_failed:{str(exc)[:160]}",
                                "watchdog": watchdog,
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
