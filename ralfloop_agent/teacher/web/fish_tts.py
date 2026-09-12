"""Bounded asynchronous Fish TTS cache for the isolated Teacher web app.

The browser never receives the Fish API key, ClusterIP, cache key, local path,
or a way to choose a voice. Only the fixed persistent reference ``peppone`` is
used by the helper process.
"""
from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
import queue
import subprocess
import threading

VOICE_ID = "peppone"
CACHE_VERSION = "fish-s2-pro-peppone-v1"


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
    ):
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.cache_dir = Path(cache_dir)
        self.python = python.strip()
        self.helper = Path(helper)
        self.enabled = bool(self.base_url and self.api_key and self.python and self.helper.is_file())
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=max(1, queue_size))
        if self.enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.cache_dir, 0o700)
            threading.Thread(target=self._worker, name="teacher-fish-tts", daemon=True).start()

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
        if not normalized or len(normalized) > 600 or not self.enabled:
            return {"status": "browser_fallback"}
        if self.ready_path(normalized):
            return {"status": "ready"}

        key = self._key(normalized)
        with self._lock:
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
            try:
                self._generate(key, text)
            finally:
                with self._lock:
                    self._pending.discard(key)
                self._queue.task_done()

    def _generate(self, key: str, text: str) -> None:
        target = self.cache_dir / f"{key}.wav"
        if self._valid_wav(target):
            return
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
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                timeout=1900,
                check=False,
            )
            if result.returncode == 0 and self._valid_wav(temporary):
                os.replace(temporary, target)
                os.chmod(target, 0o600)
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
