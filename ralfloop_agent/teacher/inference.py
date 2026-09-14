from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any, Callable, Iterator

from .service import ScheduledQwenModel


DEFAULT_SOCKET = Path(
    os.getenv(
        "RALF_TEACHER_INFERENCE_SOCKET",
        "/run/ralf-teacher-inference/inference.sock",
    )
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class TeacherInferenceClient:
    """
    Client usato dai singoli TeacherService.

    La sessione pedagogica resta locale al TeacherService/SQLite.
    La lease GPU NON appartiene alla sessione dello studente.
    """

    def __init__(
        self,
        socket_path: str | Path | None = None,
        *,
        timeout: float = 120.0,
    ) -> None:
        self.socket_path = Path(socket_path or DEFAULT_SOCKET)
        self.timeout = timeout

    def ensure_session(self, session_id: str) -> dict[str, Any]:
        # Intenzionalmente non inoltrato al daemon:
        # cambiare studente non deve cambiare lease GPU.
        return {
            "status": "shared_engine",
            "session_id": session_id,
        }

    def release_session(self, session_id: str) -> None:
        # La fine della sessione studente NON spegne Qwen.
        return None

    def __call__(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        request = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
        }
        wire = (
            json.dumps(
                request,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )

        with socket.socket(
            socket.AF_UNIX,
            socket.SOCK_STREAM,
        ) as client:
            client.settimeout(self.timeout)
            client.connect(str(self.socket_path))
            client.sendall(wire)

            stream = client.makefile("rb")
            line = stream.readline(MAX_RESPONSE_BYTES + 1)

        if not line or len(line) > MAX_RESPONSE_BYTES:
            raise RuntimeError("teacher_inference_invalid_response")

        try:
            response = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "teacher_inference_malformed_response"
            ) from exc

        if not isinstance(response, dict):
            raise RuntimeError("teacher_inference_invalid_response")

        if not response.get("ok"):
            raise RuntimeError(
                str(response.get("error") or "teacher_inference_failed")
            )

        result = response.get("result")

        if not isinstance(result, dict):
            raise RuntimeError("teacher_inference_invalid_result")

        return result

    def stream(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Iterator[dict[str, Any]]:
        request = {
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "stream": True,
        }
        wire = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(str(self.socket_path))
            client.sendall(wire)
            stream = client.makefile("rb")
            while True:
                line = stream.readline(MAX_RESPONSE_BYTES + 1)
                if not line or len(line) > MAX_RESPONSE_BYTES:
                    raise RuntimeError("teacher_inference_invalid_stream")
                try:
                    response = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("teacher_inference_malformed_response") from exc
                if not isinstance(response, dict) or not response.get("ok"):
                    raise RuntimeError(str(response.get("error") if isinstance(response, dict) else "teacher_inference_failed"))
                event = response.get("event")
                if not isinstance(event, dict) or event.get("type") not in {"delta", "done"}:
                    raise RuntimeError("teacher_inference_invalid_stream_event")
                yield event
                if event["type"] == "done":
                    return

    def close(self) -> None:
        # Nessuna connessione persistente posseduta dal client.
        return None


class SharedTeacherInferenceEngine:
    """
    Un solo ScheduledQwenModel / una sola lease GPU.

    Tutte le richieste degli studenti vengono serializzate qui.
    """

    SHARED_SESSION_ID = "teacher-shared-engine"

    def __init__(
        self,
        backend_factory: Callable[[], Any] = ScheduledQwenModel,
    ) -> None:
        self.backend_factory = backend_factory
        self._backend: Any | None = None
        self._last_activity = 0.0
        self._lock = threading.Lock()

    def infer(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        with self._lock:
            if self._backend is None:
                self._backend = self.backend_factory()
                self._backend.ensure_session(
                    self.SHARED_SESSION_ID
                )

            try:
                result = self._backend(
                    system_prompt,
                    user_prompt,
                )
                self._last_activity = time.monotonic()
                return result
            except Exception:
                # Fail closed: non lasciare una lease GPU dubbia.
                self._close_locked()
                raise

    def stream(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Iterator[dict[str, Any]]:
        with self._lock:
            if self._backend is None:
                self._backend = self.backend_factory()
                self._backend.ensure_session(self.SHARED_SESSION_ID)
            try:
                streamer = getattr(self._backend, "stream", None)
                if not callable(streamer):
                    result = self._backend(system_prompt, user_prompt)
                    yield {"type": "delta", "text": str(result.get("response") or "")}
                    yield {"type": "done", "result": result, "metadata": {}, "model_path": "fallback"}
                else:
                    yield from streamer(system_prompt, user_prompt)
                self._last_activity = time.monotonic()
            except Exception:
                self._close_locked()
                raise

    def release_if_idle(
        self,
        idle_timeout: float,
        *,
        now: float | None = None,
    ) -> bool:
        with self._lock:
            if self._backend is None:
                return False

            current = time.monotonic() if now is None else now

            if current - self._last_activity < idle_timeout:
                return False

            self._close_locked()
            return True

    def _close_locked(self) -> None:
        backend = self._backend
        self._backend = None
        self._last_activity = 0.0

        if backend is not None:
            backend.close()

    def close(self) -> None:
        with self._lock:
            self._close_locked()


__all__ = [
    "SharedTeacherInferenceEngine",
    "TeacherInferenceClient",
]
