from __future__ import annotations

from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any


class GpuArbiterError(RuntimeError):
    code = "gpu_arbiter_error"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


class GpuArbiterBusy(GpuArbiterError):
    code = "engine_busy"


@dataclass(frozen=True)
class GpuLockStatus:
    held: bool
    stale: bool
    metadata: dict[str, Any]


def _process_start_ticks(pid: int) -> int | None:
    try:
        text = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8")
        fields = text[text.rfind(")") + 2 :].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


class InferenceGpuArbiter:
    def __init__(self, path: Path) -> None:
        self.path = path

    def acquire_fd(
        self,
        *,
        provider: str,
        mode: str,
        model_hash: str = "",
        task_id: str = "",
        pid: int | None = None,
        process_start_ticks: int | None = None,
    ) -> int:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.path.parent, 0o700)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise GpuArbiterBusy() from exc
        owner_pid = int(pid or os.getpid())
        self.write_metadata(
            fd,
            {
                "pid": owner_pid,
                "provider": provider,
                "mode": mode,
                "timestamp": int(time.time()),
                "model_hash": model_hash,
                "task_id": task_id,
                "process_start_ticks": process_start_ticks
                if process_start_ticks is not None
                else _process_start_ticks(owner_pid),
            },
        )
        return fd

    @staticmethod
    def write_metadata(fd: int, value: dict[str, Any]) -> None:
        safe = {
            key: value.get(key)
            for key in (
                "pid",
                "provider",
                "mode",
                "timestamp",
                "model_hash",
                "task_id",
                "process_start_ticks",
            )
            if value.get(key) not in (None, "")
        }
        payload = (json.dumps(safe, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, payload)
        os.fsync(fd)

    def release_fd(self, fd: int, *, unlink: bool = True) -> None:
        try:
            if unlink:
                self.path.unlink(missing_ok=True)
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def cleanup_owned(self, pid: int) -> bool:
        try:
            fd = os.open(self.path, os.O_RDWR)
        except OSError:
            return False
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return False
            metadata = self._read_fd(fd)
            if not metadata or int(metadata.get("pid") or 0) == pid:
                self.path.unlink(missing_ok=True)
                return True
            return False
        finally:
            os.close(fd)

    def status(self, *, clean_stale: bool = False) -> GpuLockStatus:
        try:
            fd = os.open(self.path, os.O_RDWR)
        except OSError:
            return GpuLockStatus(held=False, stale=False, metadata={})
        metadata: dict[str, Any] = {}
        try:
            metadata = self._read_fd(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return GpuLockStatus(held=True, stale=False, metadata=metadata)
            stale = bool(metadata)
            if clean_stale and stale:
                self.path.unlink(missing_ok=True)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return GpuLockStatus(held=False, stale=stale, metadata=metadata)
        finally:
            os.close(fd)

    @staticmethod
    def _read_fd(fd: int) -> dict[str, Any]:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 16 * 1024)
            value = json.loads(raw.decode("utf-8")) if raw else {}
        except (OSError, UnicodeDecodeError, ValueError, TypeError):
            return {}
        return value if isinstance(value, dict) else {}


__all__ = [
    "GpuArbiterBusy",
    "GpuArbiterError",
    "GpuLockStatus",
    "InferenceGpuArbiter",
]
