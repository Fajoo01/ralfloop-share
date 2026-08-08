from __future__ import annotations

from dataclasses import dataclass
import json
from threading import BoundedSemaphore, Lock
import time
from typing import Any

import requests

from .remote_mini_protocol import MiniLimits, MiniToolRequest, MiniToolResponse
from .security import validate_remote_endpoint


class RemoteMiniError(RuntimeError):
    pass


class RemoteMiniUnavailable(RemoteMiniError):
    pass


class RemoteMiniInvalidResponse(RemoteMiniError):
    pass


@dataclass
class CircuitBreaker:
    failure_threshold: int = 3
    cooldown_sec: float = 30.0
    _failures: int = 0
    _opened_at: float | None = None

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self._opened_at is None:
            return True
        if now - self._opened_at >= self.cooldown_sec:
            self._failures = 0
            self._opened_at = None
            return True
        return False

    def success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def failure(self, now: float | None = None) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic() if now is None else now


class RemoteMiniClient:
    def __init__(
        self,
        *,
        base_url: str,
        allowed_hosts: tuple[str, ...],
        timeout_sec: float = 10.0,
        max_input_bytes: int = 16_384,
        max_output_bytes: int = 8_192,
        concurrency: int = 1,
        session: requests.Session | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.base_url = validate_remote_endpoint(base_url, allowed_hosts)
        self.timeout_sec = timeout_sec
        self.max_input_bytes = max_input_bytes
        self.max_output_bytes = max_output_bytes
        self.session = session or requests.Session()
        self.breaker = breaker or CircuitBreaker()
        self._slots = BoundedSemaphore(max(1, concurrency))
        self._state_lock = Lock()

    def health(self) -> bool:
        if not self._breaker_allows():
            return False
        response = None
        try:
            response = self.session.get(f"{self.base_url}/health", timeout=(2.0, self.timeout_sec))
            ok = response.status_code == 200
        except requests.RequestException:
            ok = False
        finally:
            if response is not None:
                response.close()
        self._record(ok)
        return ok

    def call(self, task: str, input_data: dict[str, Any]) -> MiniToolResponse:
        if not self._breaker_allows():
            raise RemoteMiniUnavailable("remote_circuit_open")
        if not self._slots.acquire(blocking=False):
            raise RemoteMiniUnavailable("remote_concurrency_limit")
        try:
            request = MiniToolRequest(
                task=task,
                input=input_data,
                limits=MiniLimits(
                    max_input_bytes=self.max_input_bytes,
                    max_output_bytes=self.max_output_bytes,
                    timeout_ms=int(self.timeout_sec * 1000),
                ),
            )
            payload = request.model_dump(mode="json")
            last_error: Exception | None = None
            for attempt in range(2):
                try:
                    response = self._post(payload)
                    if response.request_id != request.request_id:
                        raise RemoteMiniInvalidResponse("remote_request_id_mismatch")
                    self._record(True)
                    return response
                except (requests.ConnectionError, requests.Timeout, RemoteMiniUnavailable) as exc:
                    last_error = exc
                    if attempt == 0:
                        continue
                    break
            self._record(False)
            raise RemoteMiniUnavailable("remote_tool_unavailable") from last_error
        finally:
            self._slots.release()

    def _post(self, payload: dict[str, Any]) -> MiniToolResponse:
        response = None
        try:
            response = self.session.post(
                f"{self.base_url}/v1/tool",
                json=payload,
                headers={"Accept": "application/json"},
                timeout=(2.0, self.timeout_sec),
                stream=True,
            )
            if response.status_code >= 500:
                raise RemoteMiniUnavailable(f"remote_http_{response.status_code}")
            if response.status_code >= 400:
                raise RemoteMiniInvalidResponse(f"remote_http_{response.status_code}")
            raw = self._bounded_body(response)
            try:
                value = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RemoteMiniInvalidResponse("remote_invalid_json") from exc
            try:
                return MiniToolResponse.model_validate(value)
            except ValueError as exc:
                raise RemoteMiniInvalidResponse("remote_invalid_schema") from exc
        finally:
            if response is not None:
                response.close()

    def _bounded_body(self, response: requests.Response) -> bytes:
        body = bytearray()
        for chunk in response.iter_content(chunk_size=4096):
            if not chunk:
                continue
            body.extend(chunk)
            if len(body) > self.max_output_bytes:
                raise RemoteMiniInvalidResponse("remote_output_too_large")
        return bytes(body)

    def _breaker_allows(self) -> bool:
        with self._state_lock:
            return self.breaker.allow()

    def _record(self, ok: bool) -> None:
        with self._state_lock:
            self.breaker.success() if ok else self.breaker.failure()
