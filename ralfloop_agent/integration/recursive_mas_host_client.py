from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import socket
import time
from typing import Any


PROTOCOL_VERSION = 1
DEFAULT_SOCKET = "/run/ralfloop/recursive-mas.sock"
DEFAULT_MAX_BYTES = 128 * 1024
FORBIDDEN_REQUEST_FIELDS = {
    "python_path",
    "executable",
    "model_path",
    "cache_path",
    "upstream_root",
    "env",
    "environment",
    "shell",
    "command",
}


@dataclass
class RecursiveMASHostClientConfig:
    socket_path: str = DEFAULT_SOCKET
    timeout_sec: float = 150.0
    max_request_bytes: int = DEFAULT_MAX_BYTES

    @classmethod
    def from_env(cls) -> "RecursiveMASHostClientConfig":
        return cls(
            socket_path=os.getenv("RALFLOOP_RECURSIVE_MAS_SOCKET", DEFAULT_SOCKET),
            timeout_sec=float(os.getenv("RALFLOOP_RECURSIVE_MAS_CLIENT_TIMEOUT_SEC", os.getenv("RALFLOOP_DOMAIN_BRIDGE_TIMEOUT_SEC", "150"))),
            max_request_bytes=int(os.getenv("RALFLOOP_RECURSIVE_MAS_MAX_REQUEST_BYTES", str(DEFAULT_MAX_BYTES))),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RecursiveMASHostClient:
    def __init__(self, config: RecursiveMASHostClientConfig | None = None) -> None:
        self.config = config or RecursiveMASHostClientConfig.from_env()

    @classmethod
    def from_env(cls) -> "RecursiveMASHostClient":
        return cls(RecursiveMASHostClientConfig.from_env())

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        validation = validate_host_request(request, self.config.max_request_bytes)
        if not validation["ok"]:
            return {**validation, "duration_ms": _elapsed_ms(started)}
        if request.get("requires_human_confirmation"):
            return {
                "protocol_version": PROTOCOL_VERSION,
                "ok": False,
                "status": "human_confirmation_required",
                "request_id": str(request.get("request_id") or ""),
                "fallback_used": False,
                "duration_ms": _elapsed_ms(started),
            }
        path = Path(self.config.socket_path)
        if not path.exists():
            return {
                "protocol_version": PROTOCOL_VERSION,
                "ok": False,
                "status": "backend_unavailable",
                "request_id": str(request.get("request_id") or ""),
                "error_type": "socket_missing",
                "error": f"socket not found: {path}",
                "fallback_used": False,
                "duration_ms": _elapsed_ms(started),
            }
        payload = json.dumps(_public_request(request), ensure_ascii=False, sort_keys=True).encode("utf-8") + b"\n"
        if len(payload) > self.config.max_request_bytes:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "ok": False,
                "status": "invalid_request",
                "request_id": str(request.get("request_id") or ""),
                "error_type": "payload_too_large",
                "fallback_used": False,
                "duration_ms": _elapsed_ms(started),
            }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.config.timeout_sec)
                sock.connect(str(path))
                sock.sendall(payload)
                raw = _read_response(sock, self.config.max_request_bytes)
        except socket.timeout:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "ok": False,
                "status": "timeout",
                "request_id": str(request.get("request_id") or ""),
                "error_type": "client_timeout",
                "fallback_used": False,
                "duration_ms": _elapsed_ms(started),
            }
        except OSError as exc:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "ok": False,
                "status": "backend_unavailable",
                "request_id": str(request.get("request_id") or ""),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "fallback_used": False,
                "duration_ms": _elapsed_ms(started),
            }
        try:
            response = json.loads(raw.decode("utf-8"))
            if not isinstance(response, dict):
                raise ValueError("response must be object")
        except Exception as exc:
            return {
                "protocol_version": PROTOCOL_VERSION,
                "ok": False,
                "status": "worker_error",
                "request_id": str(request.get("request_id") or ""),
                "error_type": "invalid_json",
                "error": str(exc),
                "fallback_used": False,
                "duration_ms": _elapsed_ms(started),
            }
        response.setdefault("duration_ms", _elapsed_ms(started))
        response.setdefault("fallback_used", False)
        return response


def validate_host_request(request: dict[str, Any], max_request_bytes: int = DEFAULT_MAX_BYTES) -> dict[str, Any]:
    if not isinstance(request, dict):
        return {"protocol_version": PROTOCOL_VERSION, "ok": False, "status": "invalid_request", "error_type": "not_object"}
    if int(request.get("protocol_version", PROTOCOL_VERSION)) != PROTOCOL_VERSION:
        return {"protocol_version": PROTOCOL_VERSION, "ok": False, "status": "invalid_request", "error_type": "unsupported_protocol_version"}
    forbidden = sorted(FORBIDDEN_REQUEST_FIELDS.intersection(request))
    if forbidden:
        return {"protocol_version": PROTOCOL_VERSION, "ok": False, "status": "invalid_request", "error_type": "forbidden_fields", "error": ",".join(forbidden)}
    goal = str(request.get("goal") or "").strip()
    if not goal:
        return {"protocol_version": PROTOCOL_VERSION, "ok": False, "status": "invalid_request", "error_type": "goal_required"}
    if len(json.dumps(_public_request(request), ensure_ascii=False).encode("utf-8")) > max_request_bytes:
        return {"protocol_version": PROTOCOL_VERSION, "ok": False, "status": "invalid_request", "error_type": "payload_too_large"}
    return {"ok": True}


def _public_request(request: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": str(request.get("request_id") or ""),
        "goal": str(request.get("goal") or ""),
        "domain_context": request.get("domain_context") if isinstance(request.get("domain_context"), dict) else {},
        "style": str(request.get("style") or "sequential_light"),
        "rounds": int(request.get("rounds") or 1),
        "requires_human_confirmation": bool(request.get("requires_human_confirmation")),
    }


def _read_response(sock: socket.socket, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        data = sock.recv(8192)
        if not data:
            break
        total += len(data)
        if total > max_bytes:
            raise OSError("response too large")
        chunks.append(data)
        if b"\n" in data:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
