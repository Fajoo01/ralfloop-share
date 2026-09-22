from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen


DEFAULTS = {
    "host": "127.0.0.1",
    "port": 19196,
    "ctx": 4096,
    "prefill_chunk": 128,
    "threads": 8,
    "tokens": 64,
    "stage_mb": 768,
    "reserve_mb": 512,
    "expert_window": 32,
    "read_threads": 4,
    "dense_readahead": False,
    "host_cache_gb": 4,
    "host_cache_pinned": False,
    "expert_chunk_cache_gb": 8,
    "expert_chunk_cache_pinned": False,
    "min_available_ram_mb": 16 * 1024,
    "min_swap_free_mb": 1024,
    "min_gpu_free_mb": 6500,
}


class ServiceConfigError(RuntimeError):
    pass

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ServiceConfigError(f"invalid_boolean:{name}")


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ServiceConfigError(f"invalid_integer:{name}") from exc
    if value <= 0:
        raise ServiceConfigError(f"non_positive:{name}")
    return value


def _env_ports(name: str, default: str) -> tuple[int, ...]:
    raw = os.getenv(name, default)
    try:
        ports = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise ServiceConfigError(f"invalid_ports:{name}") from exc
    if any(port <= 0 or port > 65535 for port in ports):
        raise ServiceConfigError(f"invalid_ports:{name}")
    return ports


def load_service_config() -> dict[str, Any]:
    binary = Path(os.getenv("BOTTAZZI_MOTOR_JUDGE_BIN", ""))
    model = Path(os.getenv("BOTTAZZI_MOTOR_JUDGE_MODEL", ""))
    pack = Path(os.getenv("BOTTAZZI_MOTOR_JUDGE_PACK", ""))
    host = os.getenv("BOTTAZZI_MOTOR_JUDGE_HOST", str(DEFAULTS["host"]))
    port = _env_int("BOTTAZZI_MOTOR_JUDGE_PORT", int(DEFAULTS["port"]))
    if host not in {"127.0.0.1", "localhost"}:
        raise ServiceConfigError("judge_must_bind_loopback")
    if port in {19194, 19195}:
        raise ServiceConfigError("reserved_production_port")
    return {
        "binary": binary,
        "model": model,
        "pack": pack,
        "host": "127.0.0.1",
        "port": port,
        "ctx": _env_int("BOTTAZZI_MOTOR_JUDGE_CTX", int(DEFAULTS["ctx"])),
        "prefill_chunk": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_PREFILL_CHUNK", int(DEFAULTS["prefill_chunk"])
        ),
        "threads": _env_int("BOTTAZZI_MOTOR_JUDGE_THREADS", int(DEFAULTS["threads"])),
        "tokens": _env_int("BOTTAZZI_MOTOR_JUDGE_TOKENS", int(DEFAULTS["tokens"])),
        "stage_mb": _env_int("BOTTAZZI_MOTOR_JUDGE_STAGE_MB", int(DEFAULTS["stage_mb"])),
        "reserve_mb": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_RESERVE_MB", int(DEFAULTS["reserve_mb"])
        ),
        "expert_window": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_EXPERT_WINDOW", int(DEFAULTS["expert_window"])
        ),
        "read_threads": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_READ_THREADS", int(DEFAULTS["read_threads"])
        ),
        "dense_readahead": _env_bool(
            "BOTTAZZI_MOTOR_JUDGE_DENSE_READAHEAD", bool(DEFAULTS["dense_readahead"])
        ),
        "host_cache_gb": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_HOST_CACHE_GB", int(DEFAULTS["host_cache_gb"])
        ),
        "host_cache_pinned": _env_bool(
            "BOTTAZZI_MOTOR_JUDGE_HOST_CACHE_PINNED", bool(DEFAULTS["host_cache_pinned"])
        ),
        "expert_chunk_cache_gb": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_EXPERT_CHUNK_CACHE_GB", int(DEFAULTS["expert_chunk_cache_gb"])
        ),
        "expert_chunk_cache_pinned": _env_bool(
            "BOTTAZZI_MOTOR_JUDGE_EXPERT_CHUNK_CACHE_PINNED", bool(DEFAULTS["expert_chunk_cache_pinned"])
        ),
        "min_available_ram_mb": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_MIN_AVAILABLE_RAM_MB", int(DEFAULTS["min_available_ram_mb"])
        ),
        "min_swap_free_mb": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_MIN_SWAP_FREE_MB", int(DEFAULTS["min_swap_free_mb"])
        ),
        "min_gpu_free_mb": _env_int(
            "BOTTAZZI_MOTOR_JUDGE_MIN_GPU_FREE_MB", int(DEFAULTS["min_gpu_free_mb"])
        ),
        "deny_active_ports": _env_ports(
            "BOTTAZZI_MOTOR_JUDGE_DENY_ACTIVE_PORTS", "19194,19195,19240"
        ),
        "ollama_url": os.getenv(
            "BOTTAZZI_MOTOR_JUDGE_OLLAMA_URL", "http://127.0.0.1:11434"
        ).rstrip("/"),
        "resource_lock_file": os.getenv(
            "BOTTAZZI_MOTOR_JUDGE_RESOURCE_LOCK_FILE", "/run/ralfloop/inference-gpu.lock"
        ),
        "lock_file": os.getenv(
            "BOTTAZZI_MOTOR_JUDGE_LOCK_FILE", "/run/ralfloop/bottazzi-motor-judge.lock"
        ),
    }


def _meminfo_mb(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            key, _, raw = line.partition(":")
            if key not in {"MemAvailable", "SwapFree"}:
                continue
            amount = raw.strip().split()[0]
            values[key] = int(amount) // 1024
    except (OSError, ValueError, IndexError):
        return {}
    return values


def _gpu_free_mb() -> int | None:
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            check=True,
            text=True,
            capture_output=True,
            timeout=3,
        )
        values = [int(line.strip()) for line in completed.stdout.splitlines() if line.strip()]
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return max(values) if values else None


def _ollama_loaded_models(base_url: str) -> list[str] | None:
    try:
        with urlopen(f"{base_url}/api/ps", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except URLError:
        return []
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return None
    result: list[str] = []
    for row in models:
        if isinstance(row, dict):
            result.append(str(row.get("name") or row.get("model") or "unknown"))
    return result


def _active_ports(ports: tuple[int, ...]) -> list[int]:
    return [port for port in ports if tcp_health("127.0.0.1", port, timeout_sec=0.1)]


def _resource_lock_available(path: str) -> bool:
    lock_path = Path(path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o660)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def resource_snapshot(config: dict[str, Any]) -> dict[str, Any]:
    memory = _meminfo_mb()
    return {
        "mem_available_mb": memory.get("MemAvailable"),
        "swap_free_mb": memory.get("SwapFree"),
        "gpu_free_mb": _gpu_free_mb(),
        "ollama_models": _ollama_loaded_models(str(config["ollama_url"])),
        "active_heavy_ports": _active_ports(tuple(config["deny_active_ports"])),
    }


def preflight(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_service_config()
    resources = resource_snapshot(cfg)
    mem_available = resources["mem_available_mb"]
    swap_free = resources["swap_free_mb"]
    gpu_free = resources["gpu_free_mb"]
    ollama_models = resources["ollama_models"]
    checks = {
        "binary": cfg["binary"].is_file() and os.access(cfg["binary"], os.X_OK),
        "model": cfg["model"].is_file(),
        "pack": cfg["pack"].is_file(),
        "loopback": cfg["host"] == "127.0.0.1",
        "port_not_reserved": cfg["port"] not in {19194, 19195},
        "memory_headroom": (
            isinstance(mem_available, int) and mem_available >= cfg["min_available_ram_mb"]
        ),
        "swap_headroom": (
            isinstance(swap_free, int) and swap_free >= cfg["min_swap_free_mb"]
        ),
        "gpu_headroom": (
            isinstance(gpu_free, int) and gpu_free >= cfg["min_gpu_free_mb"]
        ),
        "ollama_idle": ollama_models == [],
        "heavy_ports_idle": resources["active_heavy_ports"] == [],
        "resource_lock_available": _resource_lock_available(str(cfg["resource_lock_file"])),
    }
    checks["allowed"] = all(checks.values())
    return {"config": _public_config(cfg), "resources": resources, "checks": checks}


def _public_config(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "binary": str(cfg["binary"]),
        "model": str(cfg["model"]),
        "pack": str(cfg["pack"]),
        "host": cfg["host"],
        "port": cfg["port"],
        "ctx": cfg["ctx"],
        "prefill_chunk": cfg["prefill_chunk"],
        "threads": cfg["threads"],
        "tokens": cfg["tokens"],
        "stage_mb": cfg["stage_mb"],
        "reserve_mb": cfg["reserve_mb"],
        "expert_window": cfg["expert_window"],
        "read_threads": cfg["read_threads"],
        "dense_readahead": cfg["dense_readahead"],
        "host_cache_gb": cfg["host_cache_gb"],
        "host_cache_pinned": cfg["host_cache_pinned"],
        "expert_chunk_cache_gb": cfg["expert_chunk_cache_gb"],
        "expert_chunk_cache_pinned": cfg["expert_chunk_cache_pinned"],
        "min_available_ram_mb": cfg["min_available_ram_mb"],
        "min_swap_free_mb": cfg["min_swap_free_mb"],
        "min_gpu_free_mb": cfg["min_gpu_free_mb"],
        "deny_active_ports": list(cfg["deny_active_ports"]),
        "ollama_url": cfg["ollama_url"],
        "resource_lock_file": cfg["resource_lock_file"],
    }


def build_exec(config: dict[str, Any] | None = None) -> tuple[list[str], dict[str, str]]:
    cfg = config or load_service_config()
    env = dict(os.environ)
    env.update(
        {
            "DS4_LOCK_FILE": str(cfg["lock_file"]),
            "DS4_CUDA_LOW_VRAM_STAGE_MB": str(cfg["stage_mb"]),
            "DS4_CUDA_LOW_VRAM_RESERVE_MB": str(cfg["reserve_mb"]),
            "DS4_CUDA_V41_EXPERT_WINDOWS": "1",
            "DS4_CUDA_V41_EXPERT_WINDOW": str(cfg["expert_window"]),
            "DS4_CUDA_V41_EXPERT_PACKED_IO": "1",
            "DS4_CUDA_V41_EXPERT_SORTED_IO": "1",
            "DS4_CUDA_V41_EXPERT_FRAME_DIRECT": "1",
            "DS4_CUDA_V41_EXPERT_READ_THREADS": str(cfg["read_threads"]),
            "DS4_CUDA_V41_EXPERT_PACK_FILE": str(cfg["pack"]),
            "DS4_CUDA_V41_SMALL_PREFILL": "1",
            "DS4_CUDA_LOW_VRAM_DENSE_READAHEAD": (
                "1" if cfg["dense_readahead"] else "0"
            ),
            "DS4_CUDA_LOW_VRAM_HOST_CACHE_GB": str(cfg["host_cache_gb"]),
            "DS4_CUDA_LOW_VRAM_HOST_CACHE_PINNED": (
                "1" if cfg["host_cache_pinned"] else "0"
            ),
            "DS4_CUDA_HOST_EXPERT_CHUNK_CACHE_GB": str(cfg["expert_chunk_cache_gb"]),
            "DS4_CUDA_HOST_EXPERT_CHUNK_CACHE_PINNED": (
                "1" if cfg["expert_chunk_cache_pinned"] else "0"
            ),
        }
    )
    command = [
        str(cfg["binary"]),
        "-m",
        str(cfg["model"]),
        "--backend",
        "cuda",
        "--ssd-streaming",
        "--cuda-low-vram-stream",
        "--ssd-streaming-cold",
        "--ctx",
        str(cfg["ctx"]),
        "--prefill-chunk",
        str(cfg["prefill_chunk"]),
        "--threads",
        str(cfg["threads"]),
        "--tokens",
        str(cfg["tokens"]),
        "--host",
        str(cfg["host"]),
        "--port",
        str(cfg["port"]),
    ]
    return command, env


def tcp_health(host: str, port: int, timeout_sec: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True
    except OSError:
        return False


def wait_ready(config: dict[str, Any] | None = None, timeout_sec: float = 180.0) -> bool:
    cfg = config or load_service_config()
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if tcp_health(cfg["host"], cfg["port"]):
            return True
        time.sleep(1.0)
    return False


def acquire_resource_lease(config: dict[str, Any]) -> int:
    path = Path(str(config["resource_lock_file"]))
    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o660)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, f"motor-judge pid={os.getpid()}\n".encode("utf-8"))
        os.set_inheritable(fd, True)
        return fd
    except OSError as exc:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        raise ServiceConfigError("resource_lease_busy") from exc


def serve(config: dict[str, Any] | None = None) -> None:
    cfg = config or load_service_config()
    result = preflight(cfg)
    if not result["checks"]["allowed"]:
        failed = sorted(key for key, value in result["checks"].items() if not value)
        raise ServiceConfigError("preflight_failed:" + ",".join(failed))
    lease_fd = acquire_resource_lease(cfg)
    command, env = build_exec(cfg)
    env["BOTTAZZI_MOTOR_RESOURCE_LEASE_FD"] = str(lease_fd)
    os.execvpe(command[0], command, env)

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("serve")
    ready = sub.add_parser("wait-ready")
    ready.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    try:
        cfg = load_service_config()
        if args.command == "preflight":
            result = preflight(cfg)
            print(json.dumps(result, sort_keys=True, indent=2))
            return 0 if result["checks"]["allowed"] else 3
        if args.command == "wait-ready":
            ok = wait_ready(cfg, args.timeout)
            print(json.dumps({"ready": ok, **_public_config(cfg)}, sort_keys=True))
            return 0 if ok else 3
        serve(cfg)
        return 0
    except ServiceConfigError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
