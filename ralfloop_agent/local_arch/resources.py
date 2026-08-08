from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import subprocess
from typing import Iterator


RESOURCE_CLASSES = {
    "tiny_cpu": {"ram_mb": 768, "vram_mb": 0, "heavy": False},
    "small_cpu": {"ram_mb": 4096, "vram_mb": 0, "heavy": False},
    "gpu_light": {"ram_mb": 4096, "vram_mb": 2048, "heavy": False},
    "gpu_medium": {"ram_mb": 8192, "vram_mb": 5120, "heavy": True},
    "gpu_heavy": {"ram_mb": 16384, "vram_mb": 7168, "heavy": True},
    "colibri_heavy": {"ram_mb": 24576, "vram_mb": 4096, "heavy": True},
}


@dataclass(frozen=True)
class ResourceSnapshot:
    ram_available_mb: int
    swap_free_mb: int
    vram_free_mb: int | None
    gpu_utilization: int | None


@dataclass(frozen=True)
class ResourceDecision:
    allowed: bool
    reason: str
    resource_class: str
    snapshot: ResourceSnapshot


class ResourceManager:
    def __init__(self, state_root: str | Path):
        self.state_root = Path(state_root)
        self.lock_path = self.state_root / "heavy-resource.lock"

    def snapshot(self) -> ResourceSnapshot:
        meminfo: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, raw = line.split(":", 1)
            meminfo[key] = int(raw.strip().split()[0]) // 1024
        vram, utilization = self._gpu()
        return ResourceSnapshot(
            ram_available_mb=meminfo.get("MemAvailable", 0),
            swap_free_mb=meminfo.get("SwapFree", 0),
            vram_free_mb=vram,
            gpu_utilization=utilization,
        )

    def check(self, resource_class: str) -> ResourceDecision:
        if resource_class not in RESOURCE_CLASSES:
            raise ValueError("unknown_resource_class")
        need = RESOURCE_CLASSES[resource_class]
        snap = self.snapshot()
        if snap.ram_available_mb < need["ram_mb"]:
            return ResourceDecision(False, "INSUFFICIENT_RAM", resource_class, snap)
        if need["vram_mb"] and (snap.vram_free_mb is None or snap.vram_free_mb < need["vram_mb"]):
            return ResourceDecision(False, "INSUFFICIENT_VRAM", resource_class, snap)
        if need["heavy"] and snap.swap_free_mb < 512:
            return ResourceDecision(False, "SWAP_PRESSURE", resource_class, snap)
        if need["heavy"] and self._locked():
            return ResourceDecision(False, "HEAVY_WORKLOAD_BUSY", resource_class, snap)
        return ResourceDecision(True, "RESOURCE_OK", resource_class, snap)

    @contextmanager
    def claim(self, resource_class: str) -> Iterator[ResourceDecision]:
        decision = self.check(resource_class)
        if not decision.allowed:
            raise RuntimeError(decision.reason)
        self.state_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            handle.seek(0)
            handle.truncate()
            json.dump({"pid": os.getpid(), "class": resource_class}, handle)
            handle.flush()
            try:
                yield decision
            finally:
                handle.seek(0)
                handle.truncate()
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def _gpu() -> tuple[int | None, int | None]:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free,utilization.gpu", "--format=csv,noheader,nounits"],
                check=True,
                text=True,
                capture_output=True,
                timeout=2,
            )
            free, utilization = result.stdout.splitlines()[0].split(",")
            return int(free.strip()), int(utilization.strip())
        except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
            return None, None

    def _locked(self) -> bool:
        self.state_root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
            return False
