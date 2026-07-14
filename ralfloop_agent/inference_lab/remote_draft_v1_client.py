from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from uuid import uuid4

from .remote_draft_protocol_v1 import PROTOCOL_VERSION, canonical_json, sign_body, validate_token_ids
from .security import validate_remote_endpoint


class RemoteDraftV1Error(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, failure_limit: int = 3, cooldown_sec: float = 30.0) -> None:
        if failure_limit < 1 or cooldown_sec <= 0:
            raise ValueError("invalid_circuit_breaker_config")
        self.failure_limit = failure_limit
        self.cooldown_sec = cooldown_sec
        self.failures = 0
        self.opened_at: float | None = None

    def allow(self, now: float | None = None) -> bool:
        if self.opened_at is None:
            return True
        current = time.monotonic() if now is None else now
        if current - self.opened_at >= self.cooldown_sec:
            self.failures = 0
            self.opened_at = None
            return True
        return False

    def success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def failure(self, now: float | None = None) -> None:
        self.failures += 1
        if self.failures >= self.failure_limit:
            self.opened_at = time.monotonic() if now is None else now


@dataclass(frozen=True)
class RemoteDraftResult:
    token_ids: list[int]
    tokenizer_hash: str
    vocabulary_hash: str
    timings: dict[str, float]
    network_ms: float
    bytes_tx: int
    bytes_rx: int


class RemoteDraftV1Client:
    def __init__(
        self,
        *,
        base_url: str,
        allowed_hosts: tuple[str, ...],
        hmac_secret: bytes,
        model: str = "qwen2.5:0.5b",
        timeout_sec: float = 10.0,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        self.base_url = validate_remote_endpoint(base_url, allowed_hosts)
        if len(hmac_secret) < 32:
            raise ValueError("remote_draft_hmac_secret_too_short")
        self.secret = hmac_secret
        self.model = model
        self.timeout_sec = timeout_sec
        self.circuit_breaker = circuit_breaker or CircuitBreaker()

    def _post(self, path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], int, int, float]:
        if not self.circuit_breaker.allow():
            raise RemoteDraftV1Error("remote_draft_circuit_open")
        body = canonical_json(payload)
        timestamp = str(int(time.time()))
        req = urlrequest.Request(
            f"{self.base_url}{path}",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Ralf-Timestamp": timestamp,
                "X-Ralf-Signature": sign_body(self.secret, timestamp, body),
            },
            method="POST",
        )
        started = time.monotonic()
        try:
            with urlrequest.urlopen(req, timeout=self.timeout_sec) as response:
                raw = response.read(256 * 1024 + 1)
        except (urlerror.URLError, TimeoutError) as exc:
            self.circuit_breaker.failure()
            raise RemoteDraftV1Error("remote_draft_unavailable") from exc
        elapsed = (time.monotonic() - started) * 1000
        if len(raw) > 256 * 1024:
            raise RemoteDraftV1Error("remote_draft_response_too_large")
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteDraftV1Error("remote_draft_invalid_json") from exc
        if not isinstance(data, dict) or not data.get("ok"):
            self.circuit_breaker.failure()
            raise RemoteDraftV1Error(str(data.get("error") if isinstance(data, dict) else "remote_draft_invalid_response"))
        if data.get("request_id") != payload.get("request_id"):
            self.circuit_breaker.failure()
            raise RemoteDraftV1Error("remote_draft_request_id_mismatch")
        self.circuit_breaker.success()
        return data, len(body), len(raw), elapsed

    def tokenize(self, text: str) -> tuple[list[int], str, str]:
        request_id = uuid4().hex
        data, _, _, _ = self._post(
            "/v1/tokenize",
            {"protocol_version": PROTOCOL_VERSION, "request_id": request_id, "text": text},
        )
        return (
            validate_token_ids(data.get("token_ids")),
            str(data.get("tokenizer_hash") or ""),
            str(data.get("vocabulary_hash") or ""),
        )

    def draft(self, prompt_token_ids: list[int], maximum: int) -> RemoteDraftResult:
        request_id = uuid4().hex
        data, bytes_tx, bytes_rx, network_ms = self._post(
            "/v1/draft",
            {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": request_id,
                "model": self.model,
                "prompt_token_ids": validate_token_ids(prompt_token_ids),
                "max_draft_tokens": maximum,
                "temperature": 0,
            },
        )
        tokens = data.get("draft_token_ids")
        if tokens == []:
            valid_tokens: list[int] = []
        else:
            valid_tokens = validate_token_ids(tokens, maximum=maximum)
        timings = data.get("timings") if isinstance(data.get("timings"), dict) else {}
        return RemoteDraftResult(
            token_ids=valid_tokens,
            tokenizer_hash=str(data.get("tokenizer_hash") or ""),
            vocabulary_hash=str(data.get("vocabulary_hash") or ""),
            timings={key: float(value) for key, value in timings.items() if isinstance(value, (int, float))},
            network_ms=network_ms,
            bytes_tx=bytes_tx,
            bytes_rx=bytes_rx,
        )
