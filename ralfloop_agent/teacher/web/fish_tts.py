"""Bounded asynchronous Fish TTS cache for the isolated Teacher web app.

The browser never receives the Fish endpoint, API key, cache key, local path, or
a way to choose a voice. Only the fixed persistent reference ``peppone`` is used
by the helper process. Authentication is optional only for a loopback Fish
endpoint; non-loopback endpoints still require an API key.
"""
from __future__ import annotations

from hashlib import sha256
import ipaddress
import logging
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from typing import Callable

VOICE_ID = "peppone"
FISH_SERVICE = "ralf-teacher-fish15.service"
CACHE_VERSION = "fish-1.5-peppone-v1"
log = logging.getLogger("teacher.web.fish_tts")


def _loopback_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host:
            return False
        if host == "localhost":
            return True
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, ipaddress.AddressValueError):
        return False


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
        generation_timeout_seconds: float = 150.0,
        min_free_vram_mb: int = 0,
        gpu_free_mb: Callable[[], int | None] | None = None,
        manage_local_service: bool = False,
        idle_stop_seconds: float = 300.0,
        startup_wait_seconds: float = 40.0,
        service_control: Callable[[str], bool] | None = None,
        health_probe: Callable[[], bool] | None = None,
    ):
        self.base_url = base_url.strip().rstrip("/")
        self.api_key = api_key.strip()
        self.cache_dir = Path(cache_dir)
        self.python = python.strip()
        self.helper = Path(helper)
        self.failure_backoff_seconds = max(1.0, float(failure_backoff_seconds))
        self.generation_timeout_seconds = max(30.0, min(float(generation_timeout_seconds), 300.0))
        self.min_free_vram_mb = max(0, min(int(min_free_vram_mb), 8192))
        self._gpu_free_mb = gpu_free_mb or self._nvidia_free_mb
        self.manage_local_service = bool(manage_local_service) and _loopback_url(self.base_url)
        self.idle_stop_seconds = max(0.0, min(float(idle_stop_seconds), 3600.0))
        self.startup_wait_seconds = max(1.0, min(float(startup_wait_seconds), 90.0))
        self._service_control = service_control or self._systemctl_fish
        self._health_probe = health_probe or self._fish_health
        self._idle_timer: threading.Timer | None = None
        self._service_lock = threading.Lock()
        self._service_generation = 0
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
        elif not self.api_key and not _loopback_url(self.base_url):
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
            generation_timeout_seconds=float(os.environ.get("TEACHER_FISH_TIMEOUT_SECONDS", "150")),
            min_free_vram_mb=int(os.environ.get("TEACHER_FISH_MIN_FREE_VRAM_MB", "1024")),
            manage_local_service=os.environ.get("TEACHER_FISH_MANAGE_SERVICE", "0").casefold() in {"1", "true", "yes"},
            idle_stop_seconds=float(os.environ.get("TEACHER_FISH_IDLE_STOP_SECONDS", "300")),
        )

    @staticmethod
    def _nvidia_free_mb() -> int | None:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                check=True, text=True, capture_output=True, timeout=2,
            )
            return int(result.stdout.splitlines()[0].strip())
        except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
            return None

    def _local_gpu_capacity_ok(self) -> bool:
        if not _loopback_url(self.base_url) or self.min_free_vram_mb <= 0:
            return True
        free = self._gpu_free_mb()
        return free is None or free >= self.min_free_vram_mb

    def _fish_health(self) -> bool:
        base = self.base_url[:-7] if self.base_url.endswith("/v1/tts") else self.base_url
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            request = Request(base.rstrip("/") + "/v1/health", data=b"", headers=headers, method="POST")
            with urlopen(request, timeout=1.5) as response:
                return response.status == 200
        except OSError:
            return False

    @staticmethod
    def _systemctl_fish(action: str) -> bool:
        if action not in {"start", "stop"}:
            return False
        try:
            result = subprocess.run(
                ["systemctl", "--user", action, FISH_SERVICE],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _ensure_local_service(self) -> bool:
        if not self.manage_local_service:
            return True
        if self._health_probe():
            return True
        if not self._local_gpu_capacity_ok() or not self._service_control("start"):
            return False
        deadline = time.monotonic() + self.startup_wait_seconds
        while time.monotonic() < deadline:
            if self._health_probe():
                return True
            time.sleep(0.25)
        return False

    def warmup(self) -> bool:
        """Start the fixed local Fish service in the background when capacity allows."""
        if not self.enabled or not self.manage_local_service or not self._local_gpu_capacity_ok():
            return False
        self._cancel_idle_stop()
        def run() -> None:
            if self._ensure_local_service():
                self._schedule_idle_stop()
        threading.Thread(target=run, name="teacher-fish-warmup", daemon=True).start()
        return True

    def _cancel_idle_stop(self) -> None:
        if not self.manage_local_service:
            return
        with self._service_lock:
            self._service_generation += 1
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None

    def _schedule_idle_stop(self) -> None:
        if not self.manage_local_service or self.idle_stop_seconds <= 0:
            return
        with self._service_lock:
            self._service_generation += 1
            token = self._service_generation
            if self._idle_timer is not None:
                self._idle_timer.cancel()
            def stop_if_idle() -> None:
                with self._service_lock:
                    if token != self._service_generation:
                        return
                with self._lock:
                    busy = bool(self._pending)
                if busy:
                    self._schedule_idle_stop()
                    return
                self._service_control("stop")
            self._idle_timer = threading.Timer(self.idle_stop_seconds, stop_if_idle)
            self._idle_timer.daemon = True
            self._idle_timer.start()

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
        if not self._local_gpu_capacity_ok():
            log.info("fish_tts_deferred reason=insufficient_vram")
            return {"status": "unavailable"}
        self._cancel_idle_stop()

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
                self._schedule_idle_stop()

    def _generate(self, key: str, text: str) -> bool:
        target = self.cache_dir / f"{key}.wav"
        if self._valid_wav(target):
            return True
        if not self._local_gpu_capacity_ok():
            log.info("fish_tts_deferred reason=insufficient_vram_at_generation")
            return False
        if not self._ensure_local_service():
            log.info("fish_tts_deferred reason=local_service_unavailable")
            return False
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
                timeout=self.generation_timeout_seconds,
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
