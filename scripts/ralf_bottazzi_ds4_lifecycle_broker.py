#!/usr/bin/env python3
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

UNIT = "bottazzi-ds4.service"
PORT = 19194
MODEL = "deepseek-v4-flash"
SOCKET_PATH = Path("/run/ralf-bottazzi-ds4-lifecycle/control.sock")
RIZZO_SHADOW_UNIT = "ralf-browser-rizzo-shadow.service"
RIZZO_SHADOW_MARKER = Path("/run/ralf-bottazzi-ds4-lifecycle/rizzo-shadow-paused")
ALLOWED_PEER_UIDS = frozenset({1000, 1001})
MAX_REQUEST_BYTES = 256


class BrokerError(RuntimeError):
    pass


class BottazziDs4Controller:
    def __init__(self, *, run: Callable[..., Any] = subprocess.run, sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic, http_get: Callable[..., Any] = urlopen,
                 transition_timeout: float = 210.0, shadow_marker: Path = RIZZO_SHADOW_MARKER) -> None:
        self.run = run
        self.sleep = sleep
        self.monotonic = monotonic
        self.http_get = http_get
        self.transition_timeout = transition_timeout
        self.shadow_marker = Path(shadow_marker)

    def _systemctl(self, verb: str, unit: str = UNIT) -> Any:
        if unit not in {UNIT, RIZZO_SHADOW_UNIT}:
            raise BrokerError("unsupported_systemd_unit")
        if verb == "show":
            command = ["systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState", "-p", "MainPID"]
        elif verb in {"start", "stop"}:
            command = ["systemctl", verb, unit]
        else:
            raise BrokerError("unsupported_systemctl_action")
        return self.run(command, shell=False, check=True, capture_output=True, text=True, timeout=30.0)

    def _show(self, unit: str = UNIT) -> dict[str, Any]:
        result = self._systemctl("show", unit)
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        try:
            pid = int(values.get("MainPID", "0"))
        except ValueError as exc:
            raise BrokerError("invalid_main_pid") from exc
        return {"active_state": values.get("ActiveState", "unknown"), "sub_state": values.get("SubState", "unknown"), "main_pid": pid}

    def _unit_active(self, unit: str) -> bool:
        shown = self._show(unit)
        return shown["active_state"] == "active" and shown["main_pid"] > 0

    def _wait_unit_active(self, unit: str, want_active: bool, *, timeout: float = 30.0) -> None:
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            if self._unit_active(unit) is want_active:
                return
            self.sleep(0.1)
        raise BrokerError(f"systemd_unit_transition_timeout:{unit}:{int(want_active)}")

    def _pause_rizzo_shadow(self) -> bool:
        try:
            shadow_active = self._unit_active(RIZZO_SHADOW_UNIT)
        except subprocess.CalledProcessError:
            # Optional companion service: DS4 must remain usable on hosts where
            # the Rizzo shadow scorer is not installed.
            return False
        if not shadow_active:
            return False
        self._systemctl("stop", RIZZO_SHADOW_UNIT)
        self._wait_unit_active(RIZZO_SHADOW_UNIT, False)
        self.shadow_marker.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        self.shadow_marker.write_text("paused-for-ds4\n", encoding="utf-8")
        return True

    def _restore_rizzo_shadow(self) -> bool:
        if not self.shadow_marker.exists():
            return False
        self._systemctl("start", RIZZO_SHADOW_UNIT)
        self._wait_unit_active(RIZZO_SHADOW_UNIT, True)
        self.shadow_marker.unlink(missing_ok=True)
        return True

    def _healthy(self) -> bool:
        try:
            response = self.http_get(f"http://127.0.0.1:{PORT}/v1/models", timeout=3.0)
            try:
                payload = json.loads(response.read(65537))
            finally:
                response.close()
            rows = payload.get("data") if isinstance(payload, dict) else None
            return isinstance(rows, list) and any(isinstance(row, dict) and row.get("id") == MODEL for row in rows)
        except Exception:
            return False

    def status(self) -> dict[str, Any]:
        shown = self._show()
        active = shown["active_state"] == "active"
        healthy = self._healthy() if active else False
        result = {
            "ok": True,
            "action": "status",
            "active": active,
            "active_state": shown["active_state"],
            "sub_state": shown["sub_state"],
            "main_pid": shown["main_pid"],
            "port_19194": healthy,
            "model": MODEL if healthy else None,
            "rizzo_shadow_paused": self.shadow_marker.exists(),
        }
        if active and (shown["sub_state"] != "running" or shown["main_pid"] <= 0 or not healthy):
            result.update(ok=False, error="active_unit_not_ready")
        elif not active and shown["main_pid"] != 0:
            result.update(ok=False, error="inactive_unit_has_pid")
        return result

    def _wait(self, want_active: bool) -> dict[str, Any]:
        deadline = self.monotonic() + self.transition_timeout
        latest: dict[str, Any] = {}
        while self.monotonic() < deadline:
            latest = self.status()
            if latest.get("ok") and bool(latest.get("active")) is want_active:
                return latest
            self.sleep(0.25)
        raise BrokerError(f"ds4_{'start' if want_active else 'stop'}_timeout:{latest.get('error', 'state_mismatch')}")

    def dispatch(self, action: str) -> dict[str, Any]:
        before = self.status()
        if action == "status":
            return before
        if action == "start":
            need_transition = not (before.get("ok") and before.get("active"))
            paused_shadow = False
            if need_transition:
                paused_shadow = self._pause_rizzo_shadow()
                try:
                    self._systemctl("start")
                    result = self._wait(True)
                except BaseException as primary:
                    if paused_shadow:
                        try:
                            self._restore_rizzo_shadow()
                        except BaseException as restore_error:
                            primary.add_note(f"Rizzo shadow restore also failed: {restore_error!r}")
                    raise
            else:
                result = before
            result["rizzo_shadow_paused"] = self.shadow_marker.exists()
        elif action == "stop":
            if before.get("active") or not before.get("ok"):
                self._systemctl("stop")
            result = self._wait(False)
            try:
                result["rizzo_shadow_restored"] = self._restore_rizzo_shadow()
            except BaseException as exc:
                # DS4 is already safely stopped. Shadow is best-effort and must
                # never make DS4 cleanup appear uncertain.
                result["rizzo_shadow_restored"] = False
                result["rizzo_shadow_restore_error"] = type(exc).__name__
            result["rizzo_shadow_paused"] = self.shadow_marker.exists()
        elif action == "touch":
            if not (before.get("ok") and before.get("active")):
                raise BrokerError("ds4_not_ready")
            result = before
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
    if not isinstance(payload, dict) or set(payload) != {"action"}:
        raise BrokerError("invalid_fields")
    action = payload["action"]
    if action not in {"status", "start", "stop", "touch"}:
        raise BrokerError("unsupported_action")
    return action


def peer_uid_allowed(uid: int) -> bool:
    return uid in ALLOWED_PEER_UIDS


def handle_connection(conn: socket.socket, controller: BottazziDs4Controller) -> None:
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
        line, trailing = bytes(raw).split(b"\n", 1)
        if trailing:
            raise BrokerError("multiple_requests_not_allowed")
        action = parse_request(line)
        response = controller.dispatch(action)
    except (BrokerError, subprocess.SubprocessError, OSError) as exc:
        response = {"ok": False, "error": str(exc)}
        if action is not None:
            response["action"] = action
    conn.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")


def serve(path: Path, controller: BottazziDs4Controller) -> None:
    path.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    if path.exists() or path.is_socket():
        path.unlink()
    old_umask = os.umask(0o007)
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
    args = parser.parse_args()
    serve(args.socket, BottazziDs4Controller())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
