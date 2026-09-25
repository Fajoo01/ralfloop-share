from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
from typing import Any

from .browser_target_selector import BrowserTargetSelector, parse_browser_candidates


_REF_RE = re.compile(r"\be\d+\b", re.I)
_DATA_ASSIGNMENT_RE = re.compile(
    r"\b(?:testo|text|file|path)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)", re.I
)
_GENERIC_RE = re.compile(
    r"\b(?:browser|playwright|clicca|click|premi|press|scrivi|digita|type|compila|fill|"
    r"carica|upload|invia|submit|seleziona|select|su|nel|nella|sul|sulla)\b",
    re.I,
)


def _semantic_goal(goal: str) -> str:
    value = _DATA_ASSIGNMENT_RE.sub(" ", goal)
    value = _REF_RE.sub(" ", value)
    value = _GENERIC_RE.sub(" ", value)
    return " ".join(value.split()).strip(" :-;,.")


class BrowserTargetShadowObserver:
    """Non-blocking, read-only comparison of semantic target proposals.

    The observer never performs browser actions and never writes raw goals or snapshots.
    """

    def __init__(
        self,
        *,
        selector: BrowserTargetSelector | None = None,
        audit_path: str | Path | None = None,
        queue_size: int = 64,
    ) -> None:
        self.selector = selector or BrowserTargetSelector(rizzo_enabled=True)
        configured = audit_path or os.getenv(
            "RALFLOOP_BROWSER_RIZZO_SHADOW_AUDIT",
            "/home/sibilla-cumana/.local/state/ralfloop/browser-rizzo-shadow.jsonl",
        )
        self.audit_path = Path(configured).expanduser()
        self._queue: queue.Queue[tuple[str, str, str, str] | None] = queue.Queue(maxsize=max(1, int(queue_size)))
        self._write_lock = threading.Lock()
        self._thread = threading.Thread(target=self._worker, name="browser-rizzo-shadow", daemon=True)
        self._thread.start()

    @classmethod
    def from_environment(cls) -> "BrowserTargetShadowObserver":
        return cls()

    def observe(
        self,
        *,
        goal: str,
        snapshot: str,
        authoritative_target: str,
        logical_action: str,
    ) -> bool:
        semantic_goal = _semantic_goal(goal)
        if len(semantic_goal) < 2:
            return False
        try:
            self._queue.put_nowait((semantic_goal, snapshot, authoritative_target, logical_action))
        except queue.Full:
            self._write_event({
                "ts": time.time(),
                "status": "dropped_queue_full",
                "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
                "authoritative_target": authoritative_target,
                "logical_action": logical_action,
            })
            return False
        return True

    def flush(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        return self._queue.unfinished_tasks == 0

    def close(self, timeout: float = 1.0) -> None:
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            return
        self._thread.join(timeout=max(0.0, timeout))

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                semantic_goal, snapshot, authoritative_target, logical_action = item
                started = time.perf_counter()
                candidates = parse_browser_candidates(snapshot)
                try:
                    selection = self.selector.select(semantic_goal, snapshot)
                    event: dict[str, Any] = {
                        "ts": time.time(),
                        "status": "ok",
                        "goal_sha256": hashlib.sha256(semantic_goal.encode("utf-8")).hexdigest(),
                        "snapshot_sha256": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
                        "logical_action": logical_action,
                        "candidate_count": len(candidates),
                        "authoritative_target": authoritative_target,
                        "authoritative_in_shortlist": authoritative_target in selection.shortlist,
                        "proposal_target": selection.target,
                        "proposal_source": selection.source,
                        "shortlist": list(selection.shortlist),
                        "score": selection.score,
                        "margin": selection.margin,
                        "confidence": selection.confidence,
                        "reason": selection.reason,
                        "match": selection.target == authoritative_target,
                    }
                except Exception as exc:
                    event = {
                        "ts": time.time(),
                        "status": "shadow_error",
                        "goal_sha256": hashlib.sha256(semantic_goal.encode("utf-8")).hexdigest(),
                        "snapshot_sha256": hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
                        "logical_action": logical_action,
                        "candidate_count": len(candidates),
                        "authoritative_target": authoritative_target,
                        "error": type(exc).__name__,
                    }
                event["latency_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
                self._write_event(event)
            finally:
                self._queue.task_done()

    def _write_event(self, event: dict[str, Any]) -> None:
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self._write_lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(payload)


__all__ = ["BrowserTargetShadowObserver"]
