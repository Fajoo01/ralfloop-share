from __future__ import annotations

import errno
import json
from pathlib import Path
import socket
import time
from typing import Any


class BottazziDs4LifecycleError(RuntimeError):
    pass


class BottazziDs4LifecycleClient:
    ACTIONS = frozenset({"status", "start", "stop", "touch"})

    def __init__(self, socket_path: Path | str = "/run/ralf-bottazzi-ds4-lifecycle/control.sock", *, timeout: float = 210.0, max_response_bytes: int = 4096) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def _request(self, action: str) -> dict[str, Any]:
        if action not in self.ACTIONS:
            raise ValueError("unsupported_bottazzi_ds4_action")
        wire = json.dumps({"action": action}, separators=(",", ":")).encode() + b"\n"
        connect_deadline = time.monotonic() + min(max(float(self.timeout), 0.0), 2.0)
        while True:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(self.timeout)
                    client.connect(str(self.socket_path))
                    client.sendall(wire)
                    response = bytearray()
                    while len(response) <= self.max_response_bytes:
                        chunk = client.recv(min(512, self.max_response_bytes + 1 - len(response)))
                        if not chunk:
                            break
                        response.extend(chunk)
                        if b"\n" in chunk:
                            break
                break
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ECONNREFUSED} and time.monotonic() < connect_deadline:
                    time.sleep(0.02)
                    continue
                raise BottazziDs4LifecycleError(f"bottazzi_ds4_broker_unavailable:{exc}") from exc
            except TimeoutError as exc:
                raise BottazziDs4LifecycleError(f"bottazzi_ds4_broker_unavailable:{exc}") from exc
        if len(response) > self.max_response_bytes or b"\n" not in response:
            raise BottazziDs4LifecycleError("bottazzi_ds4_broker_invalid_response_size")
        line, trailing = bytes(response).split(b"\n", 1)
        if trailing:
            raise BottazziDs4LifecycleError("bottazzi_ds4_broker_multiple_responses")
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BottazziDs4LifecycleError("bottazzi_ds4_broker_malformed_response") from exc
        if not isinstance(payload, dict) or payload.get("action") != action or not isinstance(payload.get("ok"), bool):
            raise BottazziDs4LifecycleError("bottazzi_ds4_broker_invalid_response")
        if not payload["ok"]:
            raise BottazziDs4LifecycleError(f"bottazzi_ds4_broker_{action}_failed:{payload.get('error', 'unknown')}")
        if not isinstance(payload.get("active"), bool) or not isinstance(payload.get("main_pid"), int):
            raise BottazziDs4LifecycleError("bottazzi_ds4_broker_invalid_status_fields")
        return payload

    def status(self) -> dict[str, Any]: return self._request("status")
    def start(self) -> dict[str, Any]: return self._request("start")
    def touch(self) -> dict[str, Any]: return self._request("touch")
    def stop(self) -> dict[str, Any]: return self._request("stop")


__all__ = ["BottazziDs4LifecycleClient", "BottazziDs4LifecycleError"]
