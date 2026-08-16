from __future__ import annotations

import json
from pathlib import Path
import socket
from typing import Any


class AgentCpmLifecycleError(RuntimeError):
    pass


class AgentCpmLifecycleClient:
    ACTIONS = frozenset({"status", "stop", "start"})

    def __init__(self, socket_path: Path | str = "/run/ralf-agentcpm-lifecycle/control.sock", *, timeout: float = 125.0, max_response_bytes: int = 4096) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

    def _request(self, action: str) -> dict[str, Any]:
        if action not in self.ACTIONS:
            raise ValueError("unsupported_agentcpm_action")
        wire = json.dumps({"action": action}, separators=(",", ":")).encode() + b"\n"
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
        except (OSError, TimeoutError) as exc:
            raise AgentCpmLifecycleError(f"agentcpm_broker_unavailable:{exc}") from exc
        if len(response) > self.max_response_bytes or b"\n" not in response:
            raise AgentCpmLifecycleError("agentcpm_broker_invalid_response_size")
        line, trailing = bytes(response).split(b"\n", 1)
        if trailing:
            raise AgentCpmLifecycleError("agentcpm_broker_multiple_responses")
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentCpmLifecycleError("agentcpm_broker_malformed_response") from exc
        required = {"ok", "action"}
        if not isinstance(payload, dict) or not required.issubset(payload) or payload.get("action") != action or not isinstance(payload.get("ok"), bool):
            raise AgentCpmLifecycleError("agentcpm_broker_invalid_response")
        if not payload["ok"]:
            raise AgentCpmLifecycleError(f"agentcpm_broker_{action}_failed:{payload.get('error', 'unknown')}")
        if not isinstance(payload.get("active"), bool) or not isinstance(payload.get("main_pid"), int):
            raise AgentCpmLifecycleError("agentcpm_broker_invalid_status_fields")
        return payload

    def status(self) -> dict[str, Any]:
        return self._request("status")

    def stop(self) -> dict[str, Any]:
        return self._request("stop")

    def start(self) -> dict[str, Any]:
        return self._request("start")


__all__ = ["AgentCpmLifecycleClient", "AgentCpmLifecycleError"]
