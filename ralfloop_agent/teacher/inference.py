from __future__ import annotations

import json
import ipaddress
import os
from pathlib import Path
import socket
import threading
import time
from typing import Any, Callable, Iterator
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .service import ScheduledQwenModel, TEACHER_RESPONSE_SCHEMA
from src.mcp_transport import MCPClientSession, StdioMCPTransport


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


class OllamaCpuTeacherFallback:
    """CPU-only, loopback-only fallback for grounded fast Teacher requests."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv(
            "RALF_TEACHER_CPU_FALLBACK_URL", "http://127.0.0.1:11434"
        )).rstrip("/")
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 11434:
            raise RuntimeError("teacher_cpu_fallback_url_denied")
        self.model = model or os.getenv("RALF_TEACHER_CPU_FALLBACK_MODEL", "gemma3:4b")
        self.timeout = float(timeout if timeout is not None else os.getenv("RALF_TEACHER_CPU_FALLBACK_TIMEOUT", "75"))
        if not 10.0 <= self.timeout <= 180.0:
            raise RuntimeError("teacher_cpu_fallback_timeout_invalid")

    @staticmethod
    def allowed(user_prompt: str) -> bool:
        try:
            payload = json.loads(user_prompt)
        except (TypeError, json.JSONDecodeError):
            return False
        if not isinstance(payload, dict):
            return False
        pedagogy = payload.get("pedagogy")
        if isinstance(pedagogy, dict) and pedagogy.get("model_path") == "deep":
            return False
        deterministic = payload.get("deterministic_evidence")
        guarded = isinstance(deterministic, dict) and isinstance(deterministic.get("concept_evidence"), dict)
        grammar = isinstance(payload.get("grammar_evidence"), dict)
        return guarded or grammar

    @staticmethod
    def _compact_context(user_prompt: str) -> dict[str, Any]:
        try:
            payload = json.loads(user_prompt)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("teacher_cpu_fallback_invalid_context") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("teacher_cpu_fallback_invalid_context")

        student = payload.get("student") if isinstance(payload.get("student"), dict) else {}
        pedagogy = payload.get("pedagogy") if isinstance(payload.get("pedagogy"), dict) else {}
        compact: dict[str, Any] = {
            "action": payload.get("action"),
            "student": {
                "school_level": student.get("school_level"),
                "class_year": student.get("class_year"),
            },
            "session": payload.get("session"),
            "pedagogy": {
                key: pedagogy.get(key)
                for key in (
                    "mode", "strategy", "model_path", "access",
                    "target_sentences", "max_response_chars", "micro_check",
                    "require_grounding", "allow_final_solution",
                )
                if key in pedagogy
            },
            "interaction": payload.get("interaction"),
            "request": payload.get("request"),
            "deterministic_evidence": payload.get("deterministic_evidence"),
            "grammar_evidence": payload.get("grammar_evidence"),
        }
        history = payload.get("history")
        if isinstance(history, list) and history:
            compact["history"] = history[-2:]
        return {key: value for key, value in compact.items() if value not in (None, {}, [])}

    @staticmethod
    def _fallback_system_prompt() -> str:
        return (
            "Sei Bot-tazzi Teacher, tutor locale. Usa soltanto il JSON fornito. "
            "deterministic_evidence e grammar_evidence sono vincolanti: non contraddirli e non inventare fatti mancanti. "
            "Rispetta pedagogy, soprattutto mode, strategy, access, micro_check, max_response_chars e allow_final_solution. "
            "Se allow_final_solution è false, non rivelare la soluzione finale. "
            "Per generate_exercise crea un solo esercizio breve senza soluzione; per check_answer usa correct solo quando la valutazione è supportata. "
            "Segui eventuali vincoli della request su formato, HTML o Markdown. "
            "Restituisci esclusivamente JSON conforme allo schema richiesto."
        )

    @staticmethod
    def _parse_content(text: str) -> dict[str, Any]:
        if len(text) > 131072:
            raise RuntimeError("teacher_cpu_fallback_response_too_large")
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise RuntimeError("teacher_cpu_fallback_malformed_json") from exc
        if not isinstance(value, dict) or set(value) - {"response", "correct"}:
            raise RuntimeError("teacher_cpu_fallback_invalid_result")
        response = value.get("response")
        if not isinstance(response, str) or not response.strip():
            raise RuntimeError("teacher_cpu_fallback_invalid_result")
        result: dict[str, Any] = {"response": response.strip(), "_inference_path": "ollama_cpu"}
        if isinstance(value.get("correct"), bool):
            result["correct"] = value["correct"]
        return result

    def _request_payload(self, user_prompt: str) -> dict[str, Any]:
        if not self.allowed(user_prompt):
            raise RuntimeError("teacher_cpu_fallback_not_grounded")
        compact_context = self._compact_context(user_prompt)
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self._fallback_system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        compact_context,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            ],
            "stream": False,
            "format": TEACHER_RESPONSE_SCHEMA,
            "keep_alive": "30s",
            "options": {
                "num_gpu": 0,
                "num_ctx": 2048,
                "num_predict": 220,
                "temperature": 0,
            },
        }

    def _infer_endpoint(
        self,
        endpoint: str,
        user_prompt: str,
        *,
        inference_path: str = "ollama_cpu",
    ) -> dict[str, Any]:
        payload = self._request_payload(user_prompt)
        request = Request(
            endpoint.rstrip("/") + "/api/chat",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=self.timeout) as response:
            body = response.read(262145)
        if len(body) > 262144:
            raise RuntimeError("teacher_cpu_fallback_wire_too_large")
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("teacher_cpu_fallback_invalid_envelope") from exc
        content = envelope.get("message", {}).get("content") if isinstance(envelope, dict) else None
        if not isinstance(content, str):
            raise RuntimeError("teacher_cpu_fallback_invalid_envelope")
        result = self._parse_content(content)
        result["_inference_path"] = inference_path
        return result

    def infer(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        return self._infer_endpoint(self.base_url, user_prompt)

    def stream(self, system_prompt: str, user_prompt: str) -> Iterator[dict[str, Any]]:
        result = self.infer(system_prompt, user_prompt)
        yield {"type": "delta", "text": result["response"]}
        yield {"type": "done", "result": result, "metadata": {"fallback": "ollama_cpu"}, "model_path": "cpu_fallback"}

    def close(self) -> None:
        return None


class PooledOllamaCpuTeacherFallback(OllamaCpuTeacherFallback):
    """Optional bounded Gemma pool with local Ollama as fail-safe fallback."""

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        binary_path: str | Path | None = None,
        pool_session_factory: Callable[[], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        raw_config = str(
            config_path
            or os.getenv("RALF_TEACHER_MODEL_POOL_CONFIG", "")
        ).strip()
        if not raw_config:
            raise RuntimeError("teacher_model_pool_config_missing")
        self.config_path = Path(raw_config)
        self.binary_path = Path(
            binary_path
            or os.getenv(
                "RALF_TEACHER_MODEL_POOL_BINARY",
                "/home/sibilla-cumana/ralfloop-production/current/bin/ralf-teacher-model-pool-mcp",
            )
        )
        self._pool_session_factory = pool_session_factory
        self._pool: Any | None = None

    @staticmethod
    def _validate_endpoint(endpoint: str) -> str:
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "http"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or parsed.port is None
        ):
            raise RuntimeError("teacher_model_pool_endpoint_denied")
        host = parsed.hostname
        if host != "localhost":
            try:
                ipaddress.ip_address(host)
            except ValueError as exc:
                raise RuntimeError("teacher_model_pool_endpoint_denied") from exc
        return endpoint.rstrip("/")

    def _pool_session(self) -> Any:
        if self._pool is not None:
            return self._pool
        if self._pool_session_factory is not None:
            self._pool = self._pool_session_factory()
            return self._pool
        if not self.config_path.is_file() or not self.binary_path.is_file():
            raise RuntimeError("teacher_model_pool_unavailable")
        self._pool = MCPClientSession(
            StdioMCPTransport([
                str(self.binary_path),
                "--config",
                str(self.config_path),
                "--stdio",
            ]),
            timeout=min(10.0, self.timeout),
            client_name="teacher-inference-pool",
        )
        return self._pool

    @staticmethod
    def _pool_payload(raw: dict[str, Any]) -> dict[str, Any]:
        value = raw.get("structuredContent")
        if not isinstance(value, dict) or value.get("ok") is not True:
            raise RuntimeError("teacher_model_pool_invalid_response")
        return value

    def _reset_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            closer = getattr(pool, "close", None)
            if callable(closer):
                closer()

    def infer(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        if not self.allowed(user_prompt):
            raise RuntimeError("teacher_cpu_fallback_not_grounded")
        pool = None
        lease = None
        started = time.monotonic()
        try:
            pool = self._pool_session()
            acquired = self._pool_payload(
                pool.call_tool("pool.acquire", {"model": "gemma3:4b"})
            )
            lease = acquired.get("lease")
            node = acquired.get("node")
            endpoint = self._validate_endpoint(str(acquired.get("endpoint") or ""))
            if (
                not isinstance(lease, str)
                or not lease
                or not isinstance(node, str)
                or not node
            ):
                raise RuntimeError("teacher_model_pool_invalid_lease")
            return self._infer_endpoint(
                endpoint,
                user_prompt,
                inference_path=f"ollama_cpu_pool:{node}",
            )
        except Exception:
            return super().infer(system_prompt, user_prompt)
        finally:
            if pool is not None and isinstance(lease, str) and lease:
                latency_ms = max(0.0, (time.monotonic() - started) * 1000.0)
                try:
                    self._pool_payload(
                        pool.call_tool(
                            "pool.release",
                            {"lease": lease, "latency_ms": latency_ms},
                        )
                    )
                except Exception:
                    self._reset_pool()

    def close(self) -> None:
        self._reset_pool()
        super().close()


class SharedTeacherInferenceEngine:
    """One shared GPU backend with a grounded CPU-only emergency path."""

    SHARED_SESSION_ID = "teacher-shared-engine"

    def __init__(
        self,
        backend_factory: Callable[[], Any] = ScheduledQwenModel,
        fallback_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.backend_factory = backend_factory
        if fallback_factory is None and os.getenv("RALF_TEACHER_CPU_FALLBACK", "0") == "1":
            fallback_factory = (
                PooledOllamaCpuTeacherFallback
                if os.getenv("RALF_TEACHER_MODEL_POOL_CONFIG", "").strip()
                else OllamaCpuTeacherFallback
            )
        self._fallback = fallback_factory() if fallback_factory is not None else None
        self._backend: Any | None = None
        self._last_activity = 0.0
        self._lock = threading.Lock()

    def _ensure_backend_locked(self) -> None:
        if self._backend is not None:
            return
        backend = self.backend_factory()
        try:
            backend.ensure_session(self.SHARED_SESSION_ID)
        except Exception:
            closer = getattr(backend, "close", None)
            if callable(closer):
                closer()
            raise
        self._backend = backend

    def _fallback_allowed(self, user_prompt: str) -> bool:
        fallback = self._fallback
        if fallback is None:
            return False
        allowed = getattr(fallback, "allowed", None)
        return bool(allowed(user_prompt)) if callable(allowed) else True

    def _fallback_infer_locked(
        self,
        system_prompt: str,
        user_prompt: str,
        primary_error: Exception,
    ) -> dict[str, Any]:
        fallback = self._fallback
        if fallback is None or not self._fallback_allowed(user_prompt):
            raise primary_error
        try:
            result = fallback.infer(system_prompt, user_prompt)
        except Exception:
            raise primary_error
        self._last_activity = time.monotonic()
        return result

    def infer(self, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        with self._lock:
            try:
                self._ensure_backend_locked()
                result = self._backend(system_prompt, user_prompt)
                self._last_activity = time.monotonic()
                return result
            except Exception as exc:
                self._close_locked()
                return self._fallback_infer_locked(system_prompt, user_prompt, exc)

    def stream(self, system_prompt: str, user_prompt: str) -> Iterator[dict[str, Any]]:
        with self._lock:
            try:
                self._ensure_backend_locked()
                streamer = getattr(self._backend, "stream", None)
                if not callable(streamer):
                    result = self._backend(system_prompt, user_prompt)
                    yield {"type": "delta", "text": str(result.get("response") or "")}
                    yield {"type": "done", "result": result, "metadata": {}, "model_path": "fallback"}
                else:
                    yield from streamer(system_prompt, user_prompt)
                self._last_activity = time.monotonic()
                return
            except Exception as exc:
                self._close_locked()
                fallback = self._fallback
                if fallback is None or not self._fallback_allowed(user_prompt):
                    raise
                try:
                    fallback_stream = getattr(fallback, "stream", None)
                    if callable(fallback_stream):
                        yield from fallback_stream(system_prompt, user_prompt)
                    else:
                        result = fallback.infer(system_prompt, user_prompt)
                        yield {"type": "delta", "text": str(result.get("response") or "")}
                        yield {"type": "done", "result": result, "metadata": {"fallback": "cpu"}, "model_path": "cpu_fallback"}
                    self._last_activity = time.monotonic()
                    return
                except Exception:
                    raise exc

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
            closer = getattr(backend, "close", None)
            if callable(closer):
                closer()

    def close(self) -> None:
        with self._lock:
            self._close_locked()
            fallback = self._fallback
            if fallback is not None:
                closer = getattr(fallback, "close", None)
                if callable(closer):
                    closer()


__all__ = [
    "SharedTeacherInferenceEngine",
    "TeacherInferenceClient",
    "OllamaCpuTeacherFallback",
    "PooledOllamaCpuTeacherFallback",
]
