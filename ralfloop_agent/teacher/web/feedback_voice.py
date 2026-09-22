"""Bounded server-side voice handles for tutor-generated feedback.

The browser never submits synthesis text or chooses a voice. A handle is created
only after the Teacher server has produced the feedback text itself.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import re
import threading
import time
import uuid

log = logging.getLogger("teacher.web.feedback_voice")


def _feedback_text(value, limit: int = 4000) -> str:
    """Return plain tutor text, including from accidentally nested model JSON.

    Qwen occasionally emits a JSON-looking response string containing literal
    newlines inside the quoted ``response`` value. That is not valid JSON, so a
    normal ``json.loads`` fallback would otherwise leak the wrapper to the
    student UI and to TTS.
    """
    text = str(value or "").strip()
    for _ in range(4):
        if not text.startswith("{"):
            break
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            match = re.fullmatch(
                r"\s*\{\s*['\"](?:response|feedback)['\"]\s*:\s*(['\"])(.*)\1\s*\}\s*",
                text,
                flags=re.DOTALL,
            )
            if not match:
                break
            text = (
                match.group(2)
                .replace(r'\"', '"')
                .replace(r"\'", "'")
                .replace(r"\n", "\n")
                .strip()
            )
            continue
        if not isinstance(parsed, dict):
            break
        nested = parsed.get("response")
        if nested is None:
            nested = parsed.get("feedback")
        if nested is None:
            break
        if isinstance(nested, (dict, list)):
            text = json.dumps(nested, ensure_ascii=False)
        else:
            text = str(nested).strip()
    return text.replace("**", "").replace("__", "")[:limit]


class FeedbackVoiceRegistry:
    def __init__(
        self,
        fish,
        *,
        ttl_seconds: float = 1800.0,
        max_entries: int = 512,
        ready_wait_seconds: float = 75.0,
        ready_poll_seconds: float = 0.25,
    ):
        self.fish = fish
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self.ready_wait_seconds = max(0.0, float(ready_wait_seconds))
        self.ready_poll_seconds = max(0.05, float(ready_poll_seconds))
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

        text = _feedback_text(feedback)
        output = dict(result)
        output["feedback"] = text
        if not text:
            return output

        prepared = self._safe_prepare(text)
        status = prepared.get("status")
        # Preserve the cleaned API payload when Fish is disabled, unsuitable for
        # this text, or unavailable. Never fall back to a browser-selected voice
        # for tutor feedback.
        if status not in {"pending", "ready"}:
            return output

        voice_id = uuid.uuid4().hex
        now = time.monotonic()
        with self._lock:
            self._cleanup_locked(now)
            self._entries[voice_id] = (student_id, text, now + self.ttl_seconds)

        # The same-origin file endpoint is waitable for a bounded period. Expose
        # it immediately so the browser can start playback from the user's tutor
        # action instead of waiting 15 seconds for the first polling cycle.
        voice = {
            "id": voice_id,
            "status": "ready",
            "url": f"/api/feedback-audio/{voice_id}/file",
        }
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
        try:
            path = self.fish.ready_path(text)
        except Exception as exc:
            log.warning("feedback_audio_prepare error_class=%s", type(exc).__name__)
            path = None
        if path is not None:
            return {"status": "ready", "url": f"/api/feedback-audio/{voice_id}/file"}

        prepared = self._safe_prepare(text)
        status = prepared.get("status") or "unavailable"
        result = {"status": status}
        if status == "ready":
            result["url"] = f"/api/feedback-audio/{voice_id}/file"
        return result

    def ready_path(self, student_id: str, voice_id: str) -> Path | None:
        text = self._text(student_id, voice_id)
        deadline = time.monotonic() + self.ready_wait_seconds
        try:
            while True:
                path = self.fish.ready_path(text)
                if path is not None:
                    return path
                if time.monotonic() >= deadline:
                    return None
                time.sleep(self.ready_poll_seconds)
        except Exception as exc:
            log.warning("feedback_audio_file error_class=%s", type(exc).__name__)
            return None
