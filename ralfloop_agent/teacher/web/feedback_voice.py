"""Bounded server-side voice handles for tutor-generated feedback.

The browser never submits synthesis text or chooses a voice. A handle is created
only after the Teacher server has produced the feedback text itself.
"""
from __future__ import annotations

import logging
from pathlib import Path
import threading
import time
import uuid

log = logging.getLogger("teacher.web.feedback_voice")


class FeedbackVoiceRegistry:
    def __init__(self, fish, *, ttl_seconds: float = 1800.0, max_entries: int = 512):
        self.fish = fish
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[str, str, float]] = {}

    def _cleanup_locked(self, now: float) -> None:
        for voice_id, (_, _, expires) in list(self._entries.items()):
            if expires <= now:
                self._entries.pop(voice_id, None)
        while len(self._entries) >= self.max_entries and self._entries:
            oldest = min(self._entries, key=lambda key: self._entries[key][2])
            self._entries.pop(oldest, None)

    def _safe_prepare(self, text: str) -> dict:
        try:
            return dict(self.fish.prepare(text))
        except Exception as exc:
            log.warning("feedback_audio_prepare error_class=%s", type(exc).__name__)
            return {"status": "unavailable"}

    def attach(self, student_id: str, result):
        if not isinstance(result, dict):
            return result
        feedback = result.get("feedback")
        if not isinstance(feedback, str) or not feedback.strip():
            return result
        text = feedback.strip()
        prepared = self._safe_prepare(text)
        status = prepared.get("status")
        # Preserve existing API payloads when Fish is disabled, unsuitable for
        # this text, or unavailable. In particular, never fall back to a
        # browser-selected voice for tutor feedback.
        if status not in {"pending", "ready"}:
            return result
        voice_id = uuid.uuid4().hex
        now = time.monotonic()
        with self._lock:
            self._cleanup_locked(now)
            self._entries[voice_id] = (student_id, text, now + self.ttl_seconds)
        voice = {"id": voice_id, "status": status}
        if status == "ready":
            voice["url"] = f"/api/feedback-audio/{voice_id}/file"
        output = dict(result)
        output["voice"] = voice
        return output

    def _text(self, student_id: str, voice_id: str) -> str:
        if len(voice_id) != 32 or any(ch not in "0123456789abcdef" for ch in voice_id):
            raise LookupError("feedback_voice_unavailable")
        now = time.monotonic()
        with self._lock:
            self._cleanup_locked(now)
            entry = self._entries.get(voice_id)
            if entry is None or entry[0] != student_id:
                raise LookupError("feedback_voice_unavailable")
            return entry[1]

    def prepare(self, student_id: str, voice_id: str) -> dict:
        text = self._text(student_id, voice_id)
        prepared = self._safe_prepare(text)
        status = prepared.get("status") or "unavailable"
        result = {"status": status}
        if status == "ready":
            result["url"] = f"/api/feedback-audio/{voice_id}/file"
        return result

    def ready_path(self, student_id: str, voice_id: str) -> Path | None:
        text = self._text(student_id, voice_id)
        try:
            return self.fish.ready_path(text)
        except Exception as exc:
            log.warning("feedback_audio_file error_class=%s", type(exc).__name__)
            return None
