from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .gpt_browser_cdp import ChromeCdp, CdpError
from .gpt_frontend import GptWorkController
from .gpt_work_queue import GptJobState, GptWorkQueue


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
    ) -> None:
        self.queue = queue
        self.cdp = cdp
        self.controller = GptWorkController(queue, cdp)
        self.policy = policy or GptQueueShepherdPolicy()

    @staticmethod
    def _int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

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
            composer_chars = self._int(companion.get("composer_chars"))
            if focused:
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "focused"})
                continue
            if composer_chars:
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "composer_not_empty"})
                continue
            if bool(ui.get("response_in_progress")):
                actions.append({"job_id": job.job_id, "action": "preserved", "reason": "response_in_progress"})
                continue

            user_turns = self._int(ui.get("user_turns"))
            assistant_turns = self._int(ui.get("assistant_turns"))
            pending = bool(ui.get("response_pending"))
            idle_ms = self._int(ui.get("response_idle_ms"))
            answered = user_turns > 0 and assistant_turns >= user_turns

            completed = answered and (
                not pending or idle_ms >= self.policy.complete_idle_ms
            )
            stalled = (
                pending
                and user_turns > assistant_turns
                and idle_ms >= self.policy.stalled_idle_ms
            )

            if completed:
                self.controller.release_job(job.job_id)
                actions.append(
                    {
                        "job_id": job.job_id,
                        "action": "released",
                        "reason": "response_complete",
                        "user_turns": user_turns,
                        "assistant_turns": assistant_turns,
                        "response_idle_ms": idle_ms,
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
