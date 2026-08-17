#!/usr/bin/env python3
"""Least-privilege lifecycle broker for the fixed AgentCPM user unit."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import time
from typing import Any, Callable
from urllib.request import urlopen

UNIT = "ralfloop-agentcpm.service"
ALLOWED_UID = 1000
ALLOWED_PEER_UIDS = frozenset({1000, 1001})
SERVICE_UID = 1001
PORT = 19093
MODEL = "AgentCPM-Explore"
DEFAULT_MODEL_PATH = Path("/home/sibilla-cumana/.cache/huggingface/hub/models--openbmb--AgentCPM-Explore-GGUF/blobs/16e4f54d55b9e76a2a1636464d772b659bd352f599ef3950ed98c6a26571adea")
SOCKET_PATH = Path("/run/ralf-agentcpm-lifecycle/control.sock")
MAX_REQUEST_BYTES = 256
SYSTEMCTL_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "XDG_RUNTIME_DIR": "/run/user/1001",
    "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1001/bus",
}


class BrokerError(RuntimeError):
    pass


class AgentCpmController:
    def __init__(self, *, run: Callable[..., Any] = subprocess.run,
                 proc_root: Path = Path("/proc"), model_path: Path = DEFAULT_MODEL_PATH,
                 http_get: Callable[..., Any] = urlopen, port_probe: Callable[[int], bool] | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic,
                 transition_timeout: float = 120.0) -> None:
        self.run = run
        self.proc_root = proc_root
        self.model_path = model_path
        self.http_get = http_get
        self.port_probe = port_probe or self._port_open
        self.sleep = sleep
        self.monotonic = monotonic
        self.transition_timeout = transition_timeout

    def _systemctl(self, verb: str) -> Any:
        if verb == "show":
            command = ["systemctl", "--user", "show", UNIT, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"]
        elif verb in {"start", "stop"}:
            command = ["systemctl", "--user", verb, UNIT]
        else:
            raise BrokerError("internal_unsupported_systemctl_action")
        return self.run(command, shell=False, check=True, capture_output=True, text=True,
                        timeout=15.0, env=SYSTEMCTL_ENV.copy())

    def _show(self) -> dict[str, Any]:
        result = self._systemctl("show")
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        try:
            pid = int(values.get("MainPID", "0"))
        except ValueError as exc:
            raise BrokerError("invalid_systemctl_main_pid") from exc
        return {"active_state": values.get("ActiveState", "unknown"),
                "sub_state": values.get("SubState", "unknown"), "main_pid": pid}

    @staticmethod
    def _port_open(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            return probe.connect_ex(("127.0.0.1", port)) == 0

    def _health_model(self) -> bool:
        try:
            response = self.http_get(f"http://127.0.0.1:{PORT}/v1/models", timeout=2.0)
            try:
                payload = json.loads(response.read(65537))
            finally:
                response.close()
            data = payload.get("data") if isinstance(payload, dict) else None
            return isinstance(data, list) and any(isinstance(row, dict) and row.get("id") == MODEL for row in data)
        except Exception:
            return False

    def _process_provenance(self, pid: int) -> tuple[bool, str]:
        try:
            stat = (self.proc_root / str(pid) / "status").read_text(encoding="utf-8")
            uid_line = next(line for line in stat.splitlines() if line.startswith("Uid:"))
            uid = int(uid_line.split()[1])
            argv = (self.proc_root / str(pid) / "cmdline").read_bytes().split(b"\0")
            args = [part.decode("utf-8", "strict") for part in argv if part]
        except (OSError, StopIteration, ValueError, UnicodeDecodeError):
            return False, "process_identity_unavailable"
        if uid != SERVICE_UID:
            return False, "wrong_process_uid"
        def has_value(flag: str, expected: str) -> bool:
            return flag in args and args.index(flag) + 1 < len(args) and args[args.index(flag) + 1] == expected
        if not has_value("--port", str(PORT)):
            return False, "wrong_process_port"
        if not has_value("--alias", MODEL):
            return False, "wrong_process_alias"
        model_value = next((args[i + 1] for i, arg in enumerate(args[:-1]) if arg in {"--model", "-m"}), None)
        if model_value is None or Path(model_value) != self.model_path:
            return False, "wrong_process_model_path"
        return True, ""

    def status(self) -> dict[str, Any]:
        shown = self._show()
        active = shown["active_state"] == "active"
        pid = shown["main_pid"]
        port_open = self.port_probe(PORT)
        healthy = self._health_model()
        result = {"ok": True, "action": "status", "active": active,
                  "active_state": shown["active_state"], "sub_state": shown["sub_state"],
                  "main_pid": pid, "owner_uid": SERVICE_UID if active else None,
                  "port_19093": port_open, "model": MODEL if healthy else None}
        if active:
            valid, reason = self._process_provenance(pid) if pid > 0 else (False, "active_without_main_pid")
            if not valid or shown["sub_state"] != "running" or not port_open or not healthy:
                result.update(ok=False, error=reason or "active_unit_not_ready")
        elif pid != 0 or port_open or healthy:
            result.update(ok=False, error="inactive_state_not_quiescent")
        return result

    def _wait(self, want_active: bool) -> dict[str, Any]:
        deadline = self.monotonic() + self.transition_timeout
        latest: dict[str, Any] = {}
        while self.monotonic() < deadline:
            latest = self.status()
            if latest.get("ok") and bool(latest.get("active")) is want_active:
                return latest
            self.sleep(0.2)
        raise BrokerError(f"agentcpm_{'start' if want_active else 'stop'}_timeout:{latest.get('error', 'state_mismatch')}")

    def dispatch(self, action: str) -> dict[str, Any]:
        before = self.status()
        if action == "status":
            return before
        if action == "stop":
            if before.get("active") or not before.get("ok"):
                self._systemctl("stop")
            result = self._wait(False)
        elif action == "start":
            if not (before.get("ok") and before.get("active")):
                self._systemctl("start")
            result = self._wait(True)
        else:
            raise BrokerError("unsupported_action")
        result["action"] = action
        return result


def parse_request(raw: bytes) -> str:
    if len(raw) > MAX_REQUEST_BYTES:
        raise BrokerError("request_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrokerError("malformed_json") from exc
    if not isinstance(payload, dict):
        raise BrokerError("payload_not_object")
    if set(payload) != {"action"}:
        raise BrokerError("invalid_fields")
    if payload["action"] not in {"status", "stop", "start"}:
        raise BrokerError("unsupported_action")
    return payload["action"]


def peer_uid_allowed(uid: int) -> bool:
    return uid in ALLOWED_PEER_UIDS


def handle_connection(conn: socket.socket, controller: AgentCpmController) -> None:
    action: str | None = None
    try:
        _pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
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
    except (BrokerError, subprocess.SubprocessError, OSError) as exc:
        response = {"ok": False, "error": str(exc)}
        if action is not None:
            response["action"] = action
    conn.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")


def serve(path: Path, controller: AgentCpmController) -> None:
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", type=Path, default=SOCKET_PATH)
    parser.add_argument("--model-path", type=Path, default=DEFAULT_MODEL_PATH)
    args = parser.parse_args()
    serve(args.socket, AgentCpmController(model_path=args.model_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
