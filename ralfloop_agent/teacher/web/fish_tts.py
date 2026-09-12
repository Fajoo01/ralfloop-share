"""Bounded asynchronous Fish TTS cache for the isolated Teacher web app.

The browser never receives the Fish API key, ClusterIP, cache key, local path,
or a way to choose a voice. Only the fixed persistent reference ``peppone`` is
used by the helper process.
"""
from __future__ import annotations

from hashlib import sha256
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

VOICE_ID = "peppone"
CACHE_VERSION = "fish-s2-pro-peppone-v1"
log = logging.getLogger("teacher.web.fish_tts")


class FishTTSCache:
    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        cache_dir: str | Path,
        python: str = "",
        helper: str | Path,
        queue_size: int = 8,
        failure_backoff_seconds: float = 60.0,
    ):
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.cache_dir = Path(cache_dir)
        self.python = python.strip()
        self.helper = Path(helper)
        self.failure_backoff_seconds = max(1.0, float(failure_backoff_seconds))
        self._pending: set[str] = set()
        self._failed_until: dict[str, float] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=max(1, queue_size))
        missing = self.missing_configuration()
        self.enabled = not missing
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.cache_dir, 0o700)
            threading.Thread(target=self._worker, name="teacher-fish-tts", daemon=True).start()
            log.info("fish_tts_enabled voice=%s", VOICE_ID)
        else:
            # Names only: never log URL, API key, paths containing feedback, or text.
            log.warning("fish_tts_disabled missing=%s", ",".join(missing))

    def missing_configuration(self) -> tuple[str, ...]:
        missing = []
        if not self.base_url:
            missing.append("TEACHER_FISH_URL")
        if not self.api_key:
            missing.append("TEACHER_FISH_API_KEY")
        if not self.python:
            missing.append("TEACHER_FISH_PYTHON")
        if not self.helper.is_file():
            missing.append("TEACHER_FISH_HELPER")
        return tuple(missing)

    @classmethod
    def from_env(cls) -> "FishTTSCache":
        root = Path(__file__).resolve().parents[3]
        return cls(
            base_url=os.environ.get("TEACHER_FISH_URL", ""),
            api_key=os.environ.get("TEACHER_FISH_API_KEY", ""),
            cache_dir=os.environ.get(
                "TEACHER_FISH_CACHE_DIR",
                str(Path.home() / ".local/state/ralf-teacher-web/fish-audio"),
            ),
            python=os.environ.get("TEACHER_FISH_PYTHON", ""),
            helper=root / "scripts/ralf_teacher_fish_client.py",
        )

    @staticmethod
    def _normalized(text: str) -> str:
        return " ".join(text.split())

    def _key(self, text: str) -> str:
        normalized = self._normalized(text)
        material = "\0".join((CACHE_VERSION, VOICE_ID, normalized)).encode("utf-8")
        return sha256(material).hexdigest()

    def _path(self, text: str) -> Path:
        return self.cache_dir / f"{self._key(text)}.wav"

    @staticmethod
    def _valid_wav(path: Path) -> bool:
        try:
            if path.stat().st_size < 44:
                return False
            with path.open("rb") as stream:
                head = stream.read(12)
            return head[:4] == b"RIFF" and head[8:12] == b"WAVE"
        except OSError:
            return False

    def ready_path(self, text: str) -> Path | None:
        if not self.enabled:
            return None
        path = self._path(text)
        return path if self._valid_wav(path) else None

    def prepare(self, text: str) -> dict[str, str]:
        normalized = self._normalized(text)
        if not normalized:
            return {"status": "browser_fallback"}
        if len(normalized) > 600:
            log.info("fish_tts_skipped reason=text_too_long length=%d", len(normalized))
            return {"status": "browser_fallback"}
        if not self.enabled:
            return {"status": "browser_fallback"}
        if self.ready_path(normalized):
            return {"status": "ready"}

        key = self._key(normalized)
        now = time.monotonic()
        with self._lock:
            failed_until = self._failed_until.get(key)
            if failed_until is not None:
                if failed_until > now:
                    return {"status": "unavailable"}
                self._failed_until.pop(key, None)
            if key in self._pending:
                return {"status": "pending"}
            self._pending.add(key)
            try:
                self._queue.put_nowait((key, normalized))
            except queue.Full:
                self._pending.discard(key)
                return {"status": "browser_fallback"}
        return {"status": "pending"}

    def _worker(self) -> None:
        while True:
            key, text = self._queue.get()
            ok = False
            try:
                ok = self._generate(key, text)
            finally:
                with self._lock:
                    self._pending.discard(key)
                    if ok:
                        self._failed_until.pop(key, None)
                    else:
                        self._failed_until[key] = time.monotonic() + self.failure_backoff_seconds
                self._queue.task_done()

    def _generate(self, key: str, text: str) -> bool:
        target = self.cache_dir / f"{key}.wav"
        if self._valid_wav(target):
            return True
        temporary = self.cache_dir / f".{key}.tmp.wav"
        try:
            temporary.unlink(missing_ok=True)
            env = {
                "HOME": str(Path.home()),
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "CUDA_VISIBLE_DEVICES": "",
                "TEACHER_FISH_URL": self.base_url,
                "TEACHER_FISH_API_KEY": self.api_key,
            }
            result = subprocess.run(
                [self.python, str(self.helper), "--output", str(temporary)],
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=43300,
                check=False,
            )
            if result.returncode == 0 and self._valid_wav(temporary):
                os.replace(temporary, target)
                os.chmod(target, 0o600)
                return True
            log.warning("fish_tts_generate_failed returncode=%s", result.returncode)
            return False
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("fish_tts_generate_error error_class=%s", type(exc).__name__)
            return False
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
