from __future__ import annotations

import json
from pathlib import Path
import socket
import stat
from typing import Any


class LlamaCppLifecycleError(RuntimeError):
    pass


class LlamaCppLifecycleClient:
    ACTIONS = frozenset({"status", "stop", "start"})

    def __init__(
        self,
        socket_path: Path | str,
        *,
        timeout: float = 200.0,
        max_response_bytes: int = 8192,
        expected_server_uid: int | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.expected_server_uid = expected_server_uid

    def _request(self, action: str) -> dict[str, Any]:
        if action not in self.ACTIONS:
            raise ValueError("unsupported_llama_cpp_lifecycle_action")
        wire = json.dumps({"action": action}, separators=(",", ":")).encode() + b"\n"
        try:
            socket_stat = self.socket_path.lstat()
            if (
                not stat.S_ISSOCK(socket_stat.st_mode)
                or self.socket_path.is_symlink()
                or socket_stat.st_mode & 0o002
                or (
                    self.expected_server_uid is not None
                    and socket_stat.st_uid != self.expected_server_uid
                )
            ):
                raise LlamaCppLifecycleError("llama_cpp_lifecycle_broker_untrusted_socket")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self.timeout)
                client.connect(str(self.socket_path))
                client.sendall(wire)
                response = bytearray()
                while len(response) <= self.max_response_bytes:
                    chunk = client.recv(min(1024, self.max_response_bytes + 1 - len(response)))
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\n" in chunk:
                        break
        except LlamaCppLifecycleError:
            raise
        except (OSError, TimeoutError) as exc:
            raise LlamaCppLifecycleError(f"llama_cpp_lifecycle_broker_unavailable:{exc}") from exc
        if len(response) > self.max_response_bytes or b"\n" not in response:
            raise LlamaCppLifecycleError("llama_cpp_lifecycle_broker_invalid_response_size")
        line, trailing = bytes(response).split(b"\n", 1)
        if trailing:
            raise LlamaCppLifecycleError("llama_cpp_lifecycle_broker_multiple_responses")
        try:
            payload = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LlamaCppLifecycleError("llama_cpp_lifecycle_broker_malformed_response") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("action") != action
            or not isinstance(payload.get("ok"), bool)
        ):
            raise LlamaCppLifecycleError("llama_cpp_lifecycle_broker_invalid_response")
        if not payload["ok"]:
            raise LlamaCppLifecycleError(
                f"llama_cpp_lifecycle_broker_{action}_failed:{payload.get('error', 'unknown')}"
            )
        for field in ("active", "managed", "healthy"):
            if not isinstance(payload.get(field), bool):
                raise LlamaCppLifecycleError("llama_cpp_lifecycle_broker_invalid_status_fields")
        pid = payload.get("pid")
        if pid is not None and (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0):
            raise LlamaCppLifecycleError("llama_cpp_lifecycle_broker_invalid_pid")
        return payload

    def status(self) -> dict[str, Any]:
        return self._request("status")

    def stop(self) -> dict[str, Any]:
        return self._request("stop")

    def start(self) -> dict[str, Any]:
        return self._request("start")


__all__ = ["LlamaCppLifecycleClient", "LlamaCppLifecycleError"]
