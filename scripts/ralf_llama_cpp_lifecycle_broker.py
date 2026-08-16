#!/usr/bin/env python3
"""Least-privilege lifecycle broker for the fixed llama.cpp chat engine."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import struct
import time
from typing import Any

from ralfloop_agent.providers.llama_cpp_server import (
    LlamaCppServerError,
    LlamaCppServerConfig,
    LlamaCppServerManager,
    read_process_identity,
)


SOCKET_PATH = Path("/run/ralf-llama-cpp-lifecycle/control.sock")
MAX_REQUEST_BYTES = 256
ALLOWED_PEER_UIDS = frozenset({1000, 1001})


class BrokerError(RuntimeError):
    pass


class LlamaCppController:
    def __init__(self, manager: LlamaCppServerManager | None = None) -> None:
        self.manager = manager or LlamaCppServerManager()

    def status(self, *, action: str = "status", changed: bool = False) -> dict[str, Any]:
        status = self.manager.status()
        managed = bool(status.get("managed"))
        healthy = bool(status.get("healthy"))
        pid = status.get("pid") if managed else None
        active = managed and healthy
        result = {
            "ok": active or (not managed and not healthy and not self.manager._port_in_use()),
            "action": action,
            "active": active,
            "managed": managed,
            "healthy": healthy,
            "pid": pid,
            "changed": changed,
        }
        if not result["ok"]:
            result["error"] = (
                "llama_cpp_unmanaged_process_on_port"
                if healthy or self.manager._port_in_use()
                else "llama_cpp_managed_process_unhealthy"
            )
        elif active and isinstance(pid, int):
            identity = read_process_identity(pid)
            if identity is None:
                result.update(ok=False, error="llama_cpp_process_identity_unavailable")
            else:
                result["provenance"] = {
                    "pid": identity.pid,
                    "uid": identity.uid,
                    "start_ticks": identity.start_ticks,
                    "executable": identity.executable,
                    "argv": list(identity.argv),
                }
        return result

    def dispatch(self, action: str) -> dict[str, Any]:
        before = self.status(action=action)
        if action == "status":
            return before
        if action == "stop":
            if not before["active"]:
                if not before["ok"]:
                    raise BrokerError(str(before["error"]))
                return before
            stopped = self.manager.stop()
            result = self._wait(active=False, action=action)
            result["changed"] = bool(stopped.get("changed"))
            result["returncode"] = stopped.get("returncode")
            return result
        if action == "start":
            if before["active"]:
                return before
            if not before["ok"]:
                raise BrokerError(str(before["error"]))
            started = self.manager.start(detach=True)
            result = self._wait(active=True, action=action)
            result["changed"] = bool(started.get("server_started"))
            result["server_startup_ms"] = started.get("server_startup_ms")
            return result
        raise BrokerError("unsupported_action")

    def _wait(self, *, active: bool, action: str) -> dict[str, Any]:
        deadline = time.monotonic() + self.manager.config.startup_timeout_sec + 15.0
        latest: dict[str, Any] = {}
        while time.monotonic() < deadline:
            latest = self.status(action=action)
            if latest.get("ok") and bool(latest.get("active")) is active:
                return latest
            time.sleep(0.1)
        raise BrokerError(f"llama_cpp_{action}_timeout:{latest.get('error', 'state_mismatch')}")


def parse_request(raw: bytes) -> str:
    if len(raw) > MAX_REQUEST_BYTES:
        raise BrokerError("request_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerError("malformed_json") from exc
    if not isinstance(payload, dict) or set(payload) != {"action"}:
        raise BrokerError("invalid_fields")
    action = payload.get("action")
    if action not in {"status", "stop", "start"}:
        raise BrokerError("unsupported_action")
    return str(action)


def peer_uid_allowed(uid: int) -> bool:
    return uid in ALLOWED_PEER_UIDS


def handle_connection(conn: socket.socket, controller: LlamaCppController) -> None:
    action: str | None = None
    try:
        _pid, uid, _gid = struct.unpack(
            "3i",
            conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")),
        )
        if not peer_uid_allowed(uid):
            raise BrokerError("peer_uid_denied")
        conn.settimeout(3.0)
        raw = bytearray()
        while len(raw) <= MAX_REQUEST_BYTES:
            chunk = conn.recv(min(64, MAX_REQUEST_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            if b"\n" in chunk:
                break
        if b"\n" not in raw:
            raise BrokerError("request_not_terminated")
        line, remainder = bytes(raw).split(b"\n", 1)
        if remainder:
            raise BrokerError("multiple_requests_not_allowed")
        action = parse_request(line)
        response = controller.dispatch(action)
    except (BrokerError, LlamaCppServerError, OSError) as exc:
        response = {"ok": False, "error": str(exc)}
        if action is not None:
            response["action"] = action
    conn.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")


def serve(path: Path, controller: LlamaCppController) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if path.exists() or path.is_socket():
        path.unlink()
    old_umask = os.umask(0o117)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            os.chmod(path, 0o660)
            server.listen(8)
            while True:
                conn, _ = server.accept()
                with conn:
                    handle_connection(conn, controller)
    finally:
        os.umask(old_umask)


def build_controller() -> LlamaCppController:
    config = replace(LlamaCppServerConfig.from_env(), lifecycle_socket=None)
    return LlamaCppController(LlamaCppServerManager(config))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    args = parser.parse_args()
    serve(args.socket, build_controller())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
