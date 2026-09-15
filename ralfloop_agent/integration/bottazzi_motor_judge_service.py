from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import time
from typing import Any


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
}


class ServiceConfigError(RuntimeError):
    pass

def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ServiceConfigError(f"invalid_integer:{name}") from exc
    if value <= 0:
        raise ServiceConfigError(f"non_positive:{name}")
    return value


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
        "lock_file": os.getenv(
            "BOTTAZZI_MOTOR_JUDGE_LOCK_FILE", "/run/ralfloop/bottazzi-motor-judge.lock"
        ),
    }


def preflight(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_service_config()
    checks = {
        "binary": cfg["binary"].is_file() and os.access(cfg["binary"], os.X_OK),
        "model": cfg["model"].is_file(),
        "pack": cfg["pack"].is_file(),
        "loopback": cfg["host"] == "127.0.0.1",
        "port_not_reserved": cfg["port"] not in {19194, 19195},
    }
    checks["allowed"] = all(checks.values())
    return {"config": _public_config(cfg), "checks": checks}


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
            "DS4_CUDA_LOW_VRAM_DENSE_READAHEAD": "1",
            "DS4_CUDA_LOW_VRAM_HOST_CACHE_GB": "6",
            "DS4_CUDA_LOW_VRAM_HOST_CACHE_PINNED": "1",
            "DS4_CUDA_HOST_EXPERT_CHUNK_CACHE_GB": "16",
            "DS4_CUDA_HOST_EXPERT_CHUNK_CACHE_PINNED": "1",
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


def serve(config: dict[str, Any] | None = None) -> None:
    cfg = config or load_service_config()
    result = preflight(cfg)
    if not result["checks"]["allowed"]:
        raise ServiceConfigError("preflight_failed")
    command, env = build_exec(cfg)
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
