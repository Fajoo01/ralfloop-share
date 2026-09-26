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
GOAL_CONTINUE_MARKER = "[[BOTTAZZI_GOAL_CONTINUE]]"
STATUS_PROBE = "A che punto sei? Hai risolto? Rispondi con lo stato reale del lavoro e cosa resta da fare."
STATUS_PROBE_PENDING = "status_probe_pending"

GOAL_CONTINUATION = (
    "Continua automaticamente dal punto raggiunto verso il GOAL iniziale, senza ripetere lavoro già verificato. "
    "Mantieni il repository/issue GitHub associato come diario tecnico persistente e fonte di verità; non affidarti alla sola memoria della chat. "
    "Alla fine di questo turno usa esattamente uno di questi marker: "
    "[[BOTTAZZI_GOAL_CONTINUE]] se puoi proseguire autonomamente con altro lavoro concreto; "
    "[[BOTTAZZI_GOAL_BLOCKED]] se serve davvero un dato, permesso o intervento umano; "
    "[[BOTTAZZI_GOAL_REACHED]] solo se il GOAL è verificato integralmente."
)
STALL_RECOVERY = (
    "Riprendi dall'ultimo punto utile dopo l'interruzione, senza ripartire da zero. "
    "Verifica lo stato reale e completa il passo corrente. Alla fine usa esattamente uno dei marker "
    "[[BOTTAZZI_GOAL_CONTINUE]], [[BOTTAZZI_GOAL_BLOCKED]] o [[BOTTAZZI_GOAL_REACHED]] secondo l'esito reale."
)
EXTERNAL_ONLY_RECOVERY = (
    "Riprendi questo lavoro usando come fonte di verità soltanto riferimenti esterni verificabili: "
    "repository/issue GitHub associato, commit, test, runtime/servizi e stato reale del sistema. "
    "Non usare il testo della chat precedente, riassunti della conversazione o memoria della chat per ricostruire lo stato. "
    "Individua il prossimo passo concreto, eseguilo e verifica il risultato. Alla fine usa esattamente uno dei marker "
    "[[BOTTAZZI_GOAL_CONTINUE]], [[BOTTAZZI_GOAL_BLOCKED]] o [[BOTTAZZI_GOAL_REACHED]] secondo l'esito reale."
)


@dataclass(frozen=True)
class GptQueueShepherdPolicy:
    complete_idle_ms: int = 60_000
    status_probe_reply_idle_ms: int = 15_000
    silent_stream_stalled_ms: int = 120_000
    stalled_idle_ms: int = 180_000
    recovery_cooldown_ms: int = 90_000
    max_recoveries: int = 2

    def __post_init__(self) -> None:
        if self.complete_idle_ms < 5_000:
            raise ValueError("complete_idle_ms_too_small")
        if self.status_probe_reply_idle_ms < 5_000:
            raise ValueError("status_probe_reply_idle_ms_too_small")
        if self.status_probe_reply_idle_ms > self.complete_idle_ms:
            raise ValueError("status_probe_reply_idle_ms_after_complete_idle_ms")
        if self.silent_stream_stalled_ms < self.complete_idle_ms:
            raise ValueError("silent_stream_stalled_before_complete_idle_ms")
        if self.stalled_idle_ms < self.silent_stream_stalled_ms:
            raise ValueError("stalled_idle_ms_before_silent_stream_stalled_ms")
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
        message = f"✅ {clean_title} — completato"
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

    def _recovery_prompt(self, job_id: str) -> str:
        state = self.queue.watchdog_state(job_id)
        if state["recovery_count"] >= 1:
            return EXTERNAL_ONLY_RECOVERY
        return STALL_RECOVERY

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
        if not job.conversation_url or not job.target_id:
            return False
        old_target_id = job.target_id
        try:
            recycled = self.controller.recycle_job_target(job.job_id)
            current = self.queue.get_job(job.job_id)
            recovery = self.controller.send_message(job.job_id, self._recovery_prompt(job.job_id))
            if current.last_error == STATUS_PROBE_PENDING:
                self.queue.set_state(job.job_id, GptJobState.ACTIVE, last_error=None)
            watchdog = self.queue.mark_watchdog_recovery(job.job_id)
            actions.append(
                {
                    "job_id": job.job_id,
                    "action": "recovered",
                    "reason": reason,
                    "progress_idle_ms": idle_ms,
                    "old_target_id": old_target_id,
                    "new_target_id": current.target_id,
                    "watchdog": watchdog,
                    "recycle": recycled,
                    "recovery": recovery,
                }
            )
            return True
        except (CdpError, OSError, RuntimeError, ValueError) as exc:
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
                try:
                    rollover = self.controller.restart_job_in_new_chat(
                        job.job_id,
                        reason=reason,
                    )
                    actions.append(
                        {
                            "job_id": job.job_id,
                            "action": "recovered",
                            "reason": "stalled_rollover_new_chat",
                            "progress_idle_ms": idle_ms,
                            "old_target_id": old_target_id,
                            "new_target_id": rollover.get("new_target_id"),
                            "rollover": rollover,
                        }
                    )
                    return True
                except (CdpError, OSError, RuntimeError, ValueError) as rollover_exc:
                    actions.append(
                        {
                            "job_id": job.job_id,
                            "action": "preserved",
                            "reason": (
                                f"fresh_target_rebind_failed:{str(exc)[:80]};"
                                f"rollover_failed:{str(rollover_exc)[:80]}"
                            ),
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

        # Jobs released only because their old server conversation could not be
        # hydrated are rolled into a fresh continuation chat automatically.
        for job in list(self.queue.list_jobs()):
            if (
                job.state is GptJobState.REVIEW
                and not job.target_id
                and job.last_error in {
                    "conversation_content_unavailable_after_retries",
                    "worker_stalled_after_retries",
                }
                and job.prompt.strip()
            ):
                try:
                    rollover = self.controller.restart_job_in_new_chat(
                        job.job_id,
                        reason=job.last_error or "worker_stalled",
                    )
                    actions.append(
                        {
                            "job_id": job.job_id,
                            "action": "recovered",
                            "reason": "review_rollover_new_chat",
                            "rollover": rollover,
                        }
                    )
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    self.queue.set_state(
                        job.job_id,
                        GptJobState.REVIEW,
                        last_error=f"rollover_failed:{str(exc)[:180]}",
                    )
                    actions.append(
                        {
                            "job_id": job.job_id,
                            "action": "preserved",
                            "reason": f"review_rollover_failed:{str(exc)[:160]}",
                        }
                    )

        # If an ACTIVE chat tab was closed externally, reconcile() demotes it to
        # REVIEW with chat_not_open_locally. Recover that exact conversation
        # automatically instead of leaving a false-finished row in the queue.
        for job in list(self.queue.list_jobs()):
            if (
                job.state is GptJobState.REVIEW
                and job.last_error == "chat_not_open_locally"
                and job.conversation_url
                and not job.target_id
            ):
                gate, watchdog = self._recovery_gate(job.job_id)
                if gate == "cooldown":
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "closed_chat_recovery_cooldown",
                        "watchdog": watchdog,
                    })
                    continue
                if gate == "exhausted":
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "closed_chat_recovery_exhausted",
                        "watchdog": watchdog,
                    })
                    continue
                try:
                    result = self.controller.start_job(job.job_id, reset_watchdog=False)
                    current = self.queue.get_job(job.job_id)
                    if current.state is not GptJobState.ACTIVE or not current.target_id:
                        raise RuntimeError("closed_chat_resume_not_active")
                    watchdog = self.queue.mark_watchdog_recovery(job.job_id)
                    actions.append({
                        "job_id": job.job_id,
                        "action": "recovered",
                        "reason": "closed_chat_reopened",
                        "target_id": current.target_id,
                        "watchdog": watchdog,
                        "recovery": result,
                    })
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": f"closed_chat_reopen_failed:{str(exc)[:160]}",
                        "watchdog": self.queue.watchdog_state(job.job_id),
                    })

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
            human_composer_chars = self._int(companion.get("human_composer_chars"))
            human_composer_active = bool(companion.get("human_composer_active"))
            response_text = self._substantive_response_text(companion.get("last_assistant_text"))
            response_in_progress = bool(ui.get("response_in_progress"))
            pending = bool(ui.get("response_pending"))
            temporary_access_limited = bool(ui.get("temporary_access_limited"))
            if temporary_access_limited:
                try:
                    if hasattr(self.cdp, "set_human_queue_hold"):
                        self.cdp.set_human_queue_hold(job.target_id, True)
                except (CdpError, OSError, RuntimeError, ValueError):
                    pass
                actions.append({
                    "job_id": job.job_id,
                    "action": "preserved",
                    "reason": "temporary_access_limited",
                })
                continue
            user_turns = self._int(ui.get("user_turns"))
            assistant_turns = max(
                self._int(ui.get("assistant_turns")),
                self._int(companion.get("assistant_turns")),
            )
            tool_activity_count = self._int(ui.get("tool_activity_count"))
            idle_ms = self._idle_ms(ui)
            current_job = self.queue.get_job(job.job_id)
            saved_text = self._substantive_response_text(current_job.last_assistant_text)
            probe_state = (
                self.queue.status_probe_state(job.job_id)
                if current_job.last_error == STATUS_PROBE_PENDING
                else {"assistant_turns": 0, "user_turns": 0, "assistant_text": "", "sent_at": 0}
            )
            probe_has_baseline = int(probe_state.get("sent_at", 0) or 0) > 0
            probe_reply_advanced = (
                current_job.last_error == STATUS_PROBE_PENDING
                and probe_has_baseline
                and bool(response_text)
                and (
                    assistant_turns > int(probe_state.get("assistant_turns", 0) or 0)
                    or response_text != self._substantive_response_text(probe_state.get("assistant_text"))
                )
            )
            settle_idle_ms = (
                self.policy.status_probe_reply_idle_ms
                if probe_reply_advanced
                else self.policy.complete_idle_ms
            )
            if response_text and response_text != saved_text:
                self.queue.reset_watchdog(job.job_id)
                self.queue.reset_watchdog_transport_failures(job.job_id)
                if response_in_progress or pending:
                    self.queue.set_last_assistant_text(job.job_id, response_text)
                    saved_text = response_text
            elif (user_turns > 0 or assistant_turns > 0 or response_in_progress or pending) and idle_ms < self.policy.complete_idle_ms:
                self.queue.reset_watchdog_transport_failures(job.job_id)
            final_text = response_text or saved_text
            answered = user_turns > 0 and assistant_turns >= user_turns
            explicit_goal_reached = GOAL_MARKER in final_text
            explicit_goal_blocked = GOAL_BLOCKED_MARKER in final_text
            explicit_goal_continue = GOAL_CONTINUE_MARKER in final_text

            if (
                (explicit_goal_reached or explicit_goal_blocked or explicit_goal_continue)
                and idle_ms >= settle_idle_ms
                and not (focused and human_composer_chars)
            ):
                if current_job.last_error == STATUS_PROBE_PENDING:
                    self.queue.set_state(job.job_id, GptJobState.ACTIVE, last_error=None)
                    self.queue.clear_status_probe(job.job_id)
                if response_in_progress:
                    try:
                        stopped = self.cdp.stop_chatgpt_response(job.target_id)
                        partial = self._substantive_response_text(stopped.get("last_assistant_text"))
                        if partial:
                            final_text = partial
                    except (CdpError, OSError, RuntimeError, ValueError):
                        pass
                clean_final_text = final_text.replace(GOAL_MARKER, "").replace(GOAL_BLOCKED_MARKER, "").replace(GOAL_CONTINUE_MARKER, "").strip()
                clean_saved_text = saved_text.replace(GOAL_MARKER, "").replace(GOAL_BLOCKED_MARKER, "").replace(GOAL_CONTINUE_MARKER, "").strip()
                persisted_text = clean_final_text
                if clean_saved_text:
                    minimum_final_chars = max(120, len(clean_saved_text) // 2)
                    if not clean_final_text or len(clean_final_text) < minimum_final_chars:
                        persisted_text = clean_saved_text
                if persisted_text:
                    self.queue.set_last_assistant_text(job.job_id, persisted_text)
                if explicit_goal_blocked:
                    self.controller.release_job(job.job_id)
                    self.queue.set_state(job.job_id, GptJobState.REVIEW, last_error="goal_blocked")
                    actions.append({"job_id": job.job_id, "action": "released", "reason": "goal_blocked", "response_idle_ms": idle_ms})
                    continue
                if explicit_goal_reached:
                    notification = self.completion_notifier(job.title)
                    self.controller.release_job(job.job_id)
                    actions.append({"job_id": job.job_id, "action": "released", "reason": "goal_complete", "response_idle_ms": idle_ms, "telegram_notification": notification})
                    continue
                try:
                    continuation = self.controller.send_message(job.job_id, GOAL_CONTINUATION)
                    self.queue.reset_watchdog(job.job_id)
                    actions.append({"job_id": job.job_id, "action": "continued", "reason": "goal_continue_marker", "response_idle_ms": idle_ms, "continuation": continuation})
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    if self._is_queue_busy(exc):
                        actions.append({"job_id": job.job_id, "action": "preserved", "reason": "delivery_already_queued"})
                    elif self._is_transport_error(exc):
                        self._transport_failure(job, actions, phase="goal_continue_marker", exc=exc)
                    else:
                        actions.append({"job_id": job.job_id, "action": "preserved", "reason": f"goal_continue_failed:{str(exc)[:200]}"})
                continue

            if (
                current_job.last_error == STATUS_PROBE_PENDING
                and probe_reply_advanced
                and idle_ms >= self.policy.status_probe_reply_idle_ms
                and not (focused and human_composer_chars)
            ):
                try:
                    if response_in_progress:
                        self.cdp.stop_chatgpt_response(job.target_id)
                    continuation = self.controller.send_message(job.job_id, GOAL_CONTINUATION)
                    self.queue.set_state(job.job_id, GptJobState.ACTIVE, last_error=None)
                    self.queue.clear_status_probe(job.job_id)
                    self.queue.reset_watchdog(job.job_id)
                    actions.append({
                        "job_id": job.job_id,
                        "action": "continued",
                        "reason": "status_probe_resumed",
                        "user_turns": user_turns,
                        "assistant_turns": assistant_turns,
                        "response_idle_ms": idle_ms,
                        "continuation": continuation,
                    })
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    if self._is_queue_busy(exc):
                        actions.append({"job_id": job.job_id, "action": "preserved", "reason": "delivery_already_queued"})
                    elif self._is_transport_error(exc):
                        self._transport_failure(job, actions, phase="status_probe_resume", exc=exc)
                    else:
                        actions.append({"job_id": job.job_id, "action": "preserved", "reason": f"status_probe_resume_failed:{str(exc)[:200]}"})
                continue

            if (
                user_turns == 0
                and assistant_turns == 0
                and not response_in_progress
                and not pending
                and idle_ms >= self.policy.complete_idle_ms
            ):
                watchdog = self.queue.mark_watchdog_transport_failure(job.job_id)
                if watchdog["transport_failure_count"] > 3:
                    self.controller.release_job(job.job_id)
                    self.queue.set_state(
                        job.job_id,
                        GptJobState.REVIEW,
                        last_error="conversation_content_unavailable_after_retries",
                    )
                    actions.append({
                        "job_id": job.job_id,
                        "action": "released",
                        "reason": "conversation_content_unavailable_after_retries",
                        "watchdog": watchdog,
                    })
                    continue
                try:
                    recycled = self.controller.recycle_job_target(job.job_id)
                    actions.append({
                        "job_id": job.job_id,
                        "action": "recovered",
                        "reason": "empty_conversation_target_recycled",
                        "progress_idle_ms": idle_ms,
                        "watchdog": watchdog,
                        "recovery": recycled,
                    })
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": f"empty_conversation_recycle_failed:{str(exc)[:160]}",
                        "watchdog": watchdog,
                    })
                continue

            if human_composer_active and human_composer_chars:
                actions.append({
                    "job_id": job.job_id,
                    "action": "preserved",
                    "reason": "focused_human_draft",
                    "human_composer_chars": human_composer_chars,
                })
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

            if (
                current_job.last_error == STATUS_PROBE_PENDING
                and not probe_has_baseline
                and idle_ms >= self.policy.complete_idle_ms
            ):
                try:
                    if response_in_progress:
                        reprobe = self.cdp.wake_stalled_chatgpt(job.target_id, text=STATUS_PROBE)
                    else:
                        reprobe = self.controller.send_message(job.job_id, STATUS_PROBE)
                    self.queue.set_status_probe_baseline(
                        job.job_id,
                        assistant_turns=assistant_turns,
                        user_turns=user_turns,
                        assistant_text=final_text,
                    )
                    watchdog = self.queue.mark_watchdog_recovery(job.job_id)
                    actions.append({
                        "job_id": job.job_id,
                        "action": "recovered",
                        "reason": "status_probe_rebaselined",
                        "progress_idle_ms": idle_ms,
                        "watchdog": watchdog,
                        "recovery": reprobe,
                    })
                except (CdpError, OSError, RuntimeError, ValueError) as exc:
                    if self._is_queue_busy(exc):
                        actions.append({"job_id": job.job_id, "action": "preserved", "reason": "delivery_already_queued"})
                    elif self._is_transport_error(exc):
                        self._transport_failure(job, actions, phase="status_probe_rebaseline", exc=exc)
                    else:
                        actions.append({"job_id": job.job_id, "action": "preserved", "reason": f"status_probe_rebaseline_failed:{str(exc)[:200]}"})
                continue

            if response_in_progress:
                silent_stream = assistant_turns < user_turns and tool_activity_count == 0
                stream_stall_limit_ms = (
                    self.policy.silent_stream_stalled_ms
                    if silent_stream
                    else self.policy.stalled_idle_ms
                )
                if idle_ms < stream_stall_limit_ms:
                    actions.append({
                        "job_id": job.job_id,
                        "action": "preserved",
                        "reason": "response_in_progress_silent" if silent_stream else "response_in_progress",
                        "progress_idle_ms": idle_ms,
                        "stall_limit_ms": stream_stall_limit_ms,
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
                    wake = {"submitted": False, "reason": "status_probe_already_pending"}
                    if current_job.last_error != STATUS_PROBE_PENDING:
                        wake = self.cdp.wake_stalled_chatgpt(job.target_id, text=STATUS_PROBE)
                        if bool(wake.get("submitted")):
                            self.queue.set_status_probe_baseline(
                                job.job_id,
                                assistant_turns=assistant_turns,
                                user_turns=user_turns,
                                assistant_text=final_text,
                            )
                            self.queue.set_state(job.job_id, GptJobState.ACTIVE, last_error=STATUS_PROBE_PENDING)
                            watchdog = self.queue.mark_watchdog_recovery(job.job_id)
                            actions.append({
                                "job_id": job.job_id,
                                "action": "recovered",
                                "reason": "stalled_stream_status_probe",
                                "progress_idle_ms": idle_ms,
                                "watchdog": watchdog,
                                "wake": wake,
                            })
                            continue
                    stopped = self.cdp.stop_chatgpt_response(job.target_id)
                    partial = self._substantive_response_text(stopped.get("last_assistant_text"))
                    if partial:
                        self.queue.set_last_assistant_text(job.job_id, partial)
                    if current_job.last_error == STATUS_PROBE_PENDING:
                        self.queue.set_state(job.job_id, GptJobState.ACTIVE, last_error=None)
                        self.queue.clear_status_probe(job.job_id)
                    continuation = self.controller.send_message(job.job_id, self._recovery_prompt(job.job_id))
                    watchdog = self.queue.mark_watchdog_recovery(job.job_id)
                    actions.append({
                        "job_id": job.job_id,
                        "action": "recovered",
                        "reason": "stalled_stream_restarted",
                        "progress_idle_ms": idle_ms,
                        "watchdog": watchdog,
                        "wake": wake,
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
            silent_pending = (
                pending
                and user_turns > assistant_turns
                and tool_activity_count == 0
            )
            stalled_limit_ms = (
                self.policy.silent_stream_stalled_ms
                if silent_pending
                else self.policy.stalled_idle_ms
            )
            stalled = (
                idle_ms >= stalled_limit_ms
                and (pending or user_turns > assistant_turns or not final_text)
            )

            if completed:
                goal_reached = GOAL_MARKER in final_text
                goal_blocked = GOAL_BLOCKED_MARKER in final_text
                goal_continue = GOAL_CONTINUE_MARKER in final_text
                if current_job.last_error == STATUS_PROBE_PENDING and not (goal_reached or goal_blocked or goal_continue):
                    try:
                        self.queue.set_state(job.job_id, GptJobState.ACTIVE, last_error=None)
                        continuation = self.controller.send_message(job.job_id, GOAL_CONTINUATION)
                        self.queue.clear_status_probe(job.job_id)
                        self.queue.reset_watchdog(job.job_id)
                        actions.append({
                            "job_id": job.job_id,
                            "action": "continued",
                            "reason": "status_probe_resumed",
                            "user_turns": user_turns,
                            "assistant_turns": assistant_turns,
                            "response_idle_ms": idle_ms,
                            "continuation": continuation,
                        })
                    except (CdpError, OSError, RuntimeError, ValueError) as exc:
                        if self._is_queue_busy(exc):
                            actions.append({"job_id": job.job_id, "action": "preserved", "reason": "delivery_already_queued"})
                        elif self._is_transport_error(exc):
                            self._transport_failure(job, actions, phase="status_probe_resume", exc=exc)
                        else:
                            actions.append({"job_id": job.job_id, "action": "preserved", "reason": f"status_probe_resume_failed:{str(exc)[:200]}"})
                    continue
                clean_final_text = (
                    final_text.replace(GOAL_MARKER, "").replace(GOAL_BLOCKED_MARKER, "").replace(GOAL_CONTINUE_MARKER, "").strip()
                )
                clean_saved_text = (
                    saved_text.replace(GOAL_MARKER, "").replace(GOAL_BLOCKED_MARKER, "").replace(GOAL_CONTINUE_MARKER, "").strip()
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
                if goal_reached:
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
                if goal_continue:
                    try:
                        continuation = self.controller.send_message(job.job_id, GOAL_CONTINUATION)
                        self.queue.reset_watchdog(job.job_id)
                        actions.append(
                            {
                                "job_id": job.job_id,
                                "action": "continued",
                                "reason": "goal_continue_marker",
                                "user_turns": user_turns,
                                "assistant_turns": assistant_turns,
                                "response_idle_ms": idle_ms,
                                "continuation": continuation,
                            }
                        )
                    except (CdpError, OSError, RuntimeError, ValueError) as exc:
                        if self._is_queue_busy(exc):
                            actions.append({"job_id": job.job_id, "action": "preserved", "reason": "delivery_already_queued"})
                        elif self._is_transport_error(exc):
                            self._transport_failure(job, actions, phase="goal_continue_marker", exc=exc)
                        else:
                            actions.append({"job_id": job.job_id, "action": "preserved", "reason": f"goal_continue_failed:{str(exc)[:200]}"})
                    continue
                self.controller.release_job(job.job_id)
                self.queue.set_state(job.job_id, GptJobState.REVIEW, last_error="goal_status_missing")
                actions.append(
                    {
                        "job_id": job.job_id,
                        "action": "released",
                        "reason": "goal_status_missing",
                        "user_turns": user_turns,
                        "assistant_turns": assistant_turns,
                        "response_idle_ms": idle_ms,
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
                    probing = current_job.last_error != STATUS_PROBE_PENDING
                    message = STATUS_PROBE if probing else self._recovery_prompt(job.job_id)
                    if not probing:
                        self.queue.set_state(job.job_id, GptJobState.ACTIVE, last_error=None)
                        self.queue.clear_status_probe(job.job_id)
                    recovery = self.controller.send_message(job.job_id, message)
                    if probing:
                        self.queue.set_status_probe_baseline(
                            job.job_id,
                            assistant_turns=assistant_turns,
                            user_turns=user_turns,
                            assistant_text=final_text,
                        )
                        self.queue.set_state(job.job_id, GptJobState.ACTIVE, last_error=STATUS_PROBE_PENDING)
                    watchdog = self.queue.mark_watchdog_recovery(job.job_id)
                    actions.append(
                        {
                            "job_id": job.job_id,
                            "action": "recovered",
                            "reason": "unanswered_status_probe" if probing else "unanswered_restarted",
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
