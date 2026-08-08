from __future__ import annotations

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from .gguf_tokenizer import GGUFTokenizer
from .remote_draft_protocol_v1 import (
    DraftProtocolError,
    MAX_BODY_BYTES,
    PROTOCOL_VERSION,
    canonical_json,
    parse_draft_request,
    parse_tool_request,
    parse_tokenize_request,
    verify_signature,
)


@dataclass(frozen=True)
class DraftServiceConfig:
    host: str
    port: int
    model: str
    model_path: str
    ollama_url: str
    hmac_secret: bytes
    timeout_sec: float = 20.0

    @classmethod
    def from_env(cls) -> "DraftServiceConfig":
        secret = os.getenv("RALF_DRAFTD_HMAC_SECRET", "").encode("utf-8")
        if len(secret) < 32:
            raise ValueError("draftd_hmac_secret_too_short")
        host = os.getenv("RALF_DRAFTD_HOST", "127.0.0.1")
        if host == "0.0.0.0" or host == "::":
            raise ValueError("draftd_public_bind_forbidden")
        model = os.getenv("RALF_DRAFTD_MODEL", "qwen2.5:0.5b")
        if model != "qwen2.5:0.5b":
            raise ValueError("draftd_model_not_allowed")
        return cls(
            host=host,
            port=int(os.getenv("RALF_DRAFTD_PORT", "19092")),
            model=model,
            model_path=os.environ["RALF_DRAFTD_MODEL_PATH"],
            ollama_url=os.getenv("RALF_DRAFTD_OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
            hmac_secret=secret,
            timeout_sec=float(os.getenv("RALF_DRAFTD_TIMEOUT", "20")),
        )


class OllamaDraftBackend:
    def __init__(self, config: DraftServiceConfig, tokenizer: GGUFTokenizer) -> None:
        self.config = config
        self.tokenizer = tokenizer

    def draft(self, prompt_token_ids: list[int], maximum: int) -> tuple[list[int], dict[str, float]]:
        prompt = self.tokenizer.decode(prompt_token_ids)
        payload = canonical_json(
            {
                "model": self.config.model,
                "prompt": prompt,
                "raw": True,
                "stream": False,
                "keep_alive": 0,
                "options": {"temperature": 0, "num_predict": maximum, "seed": 42},
            }
        )
        started = time.monotonic()
        req = urlrequest.Request(
            f"{self.config.ollama_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=self.config.timeout_sec) as response:
                raw = response.read(256 * 1024 + 1)
        except (urlerror.URLError, TimeoutError) as exc:
            raise DraftProtocolError("ollama_unavailable") from exc
        if len(raw) > 256 * 1024:
            raise DraftProtocolError("ollama_response_too_large")
        try:
            result = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DraftProtocolError("ollama_invalid_response") from exc
        output = str(result.get("response") or "")
        tokens = self.tokenizer.encode(output)[:maximum]
        elapsed_ms = (time.monotonic() - started) * 1000
        inference_ms = (int(result.get("prompt_eval_duration") or 0) + int(result.get("eval_duration") or 0)) / 1_000_000
        return tokens, {"queue_ms": max(0.0, elapsed_ms - inference_ms), "inference_ms": inference_ms}

    def tool(self, task: str, payload: dict[str, Any], maximum_bytes: int) -> tuple[dict[str, Any], dict[str, float]]:
        system = (
            "You are a non-binding read-only advisory tool. Return one compact JSON object. "
            "Never authorize, approve, execute, promote, or propose shell commands. "
            "Use only supplied input; mark uncertainty explicitly."
        )
        prompt = canonical_json({"task": task, "input": payload}).decode("utf-8")
        body = canonical_json(
            {
                "model": self.config.model,
                "system": system,
                "prompt": prompt,
                "stream": False,
                "keep_alive": 0,
                "format": "json",
                "options": {"temperature": 0, "num_predict": min(512, maximum_bytes // 4), "seed": 42},
            }
        )
        started = time.monotonic()
        req = urlrequest.Request(
            f"{self.config.ollama_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urlrequest.urlopen(req, timeout=self.config.timeout_sec) as response:
                raw = response.read(256 * 1024 + 1)
        except (urlerror.URLError, TimeoutError) as exc:
            raise DraftProtocolError("ollama_unavailable") from exc
        if len(raw) > 256 * 1024:
            raise DraftProtocolError("ollama_response_too_large")
        try:
            outer = json.loads(raw)
            result = json.loads(str(outer.get("response") or "{}"))
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError) as exc:
            raise DraftProtocolError("ollama_invalid_tool_response") from exc
        if not isinstance(result, dict) or len(canonical_json(result)) > maximum_bytes:
            raise DraftProtocolError("invalid_tool_result")
        forbidden = ("auto_approve", "auto_execute", "execute_approved", "promote_domain", "run_domain_canary", "apply_domain_source_update", "command", "shell")
        serialized = canonical_json(result).decode("utf-8").lower()
        if any(marker in serialized for marker in forbidden):
            raise DraftProtocolError("binding_tool_result_forbidden")
        elapsed_ms = (time.monotonic() - started) * 1000
        inference_ms = (int(outer.get("prompt_eval_duration") or 0) + int(outer.get("eval_duration") or 0)) / 1_000_000
        return result, {"queue_ms": max(0.0, elapsed_ms - inference_ms), "inference_ms": inference_ms}


class DraftApplication:
    def __init__(self, config: DraftServiceConfig, tokenizer: GGUFTokenizer | None = None, backend: Any | None = None) -> None:
        self.config = config
        self.tokenizer = tokenizer or GGUFTokenizer.from_file(config.model_path)
        self.backend = backend or OllamaDraftBackend(config, self.tokenizer)

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        if method == "GET" and path == "/health":
            identity = self.tokenizer.identity
            return 200, {
                "ok": True,
                "protocol_version": PROTOCOL_VERSION,
                "model": self.config.model,
                "tokenizer_hash": identity.tokenizer_hash,
                "vocabulary_hash": identity.vocabulary_hash,
                "vocabulary_size": identity.vocabulary_size,
            }
        if method != "POST" or path not in {"/v1/tokenize", "/v1/draft", "/v1/tool"}:
            return 404, {"ok": False, "error": "not_found"}
        try:
            verify_signature(
                self.config.hmac_secret,
                headers.get("x-ralf-timestamp", ""),
                body,
                headers.get("x-ralf-signature", ""),
            )
            if path == "/v1/tokenize":
                payload = parse_tokenize_request(body)
                return 200, {
                    "protocol_version": PROTOCOL_VERSION,
                    "request_id": payload["request_id"],
                    "ok": True,
                    "token_ids": self.tokenizer.encode(payload["text"]),
                    "tokenizer_hash": self.tokenizer.identity.tokenizer_hash,
                    "vocabulary_hash": self.tokenizer.identity.vocabulary_hash,
                }
            if path == "/v1/tool":
                payload = parse_tool_request(body)
                result, timings = self.backend.tool(payload["task"], payload["input"], payload["max_output_bytes"])
                return 200, {
                    "protocol_version": "ralf-mini-v1",
                    "request_id": payload["request_id"],
                    "ok": True,
                    "result": result,
                    "timings": timings,
                }
            payload = parse_draft_request(body, fixed_model=self.config.model)
            started = time.monotonic()
            tokens, timings = self.backend.draft(payload["prompt_token_ids"], payload["max_draft_tokens"])
            serialization_started = time.monotonic()
            response = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": payload["request_id"],
                "ok": True,
                "draft_token_ids": tokens,
                "finish_reason": None,
                "tokenizer_hash": self.tokenizer.identity.tokenizer_hash,
                "vocabulary_hash": self.tokenizer.identity.vocabulary_hash,
                "timings": {
                    "queue_ms": float(timings.get("queue_ms", 0.0)),
                    "inference_ms": float(timings.get("inference_ms", 0.0)),
                    "serialization_ms": (time.monotonic() - serialization_started) * 1000,
                    "total_ms": (time.monotonic() - started) * 1000,
                },
            }
            return 200, response
        except DraftProtocolError as exc:
            auth_errors = {"invalid_hmac_timestamp", "invalid_hmac_signature", "expired_hmac_timestamp"}
            return 400 if str(exc) not in auth_errors else 401, {"ok": False, "error": str(exc)}
        except Exception:
            return 500, {"ok": False, "error": "internal_error"}


def make_handler(application: DraftApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "ralf-draftd/1"

        def _send(self, status: int, payload: dict[str, Any]) -> None:
            raw = canonical_json(payload)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            status, payload = application.handle("GET", self.path, {}, b"")
            self._send(status, payload)

        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = MAX_BODY_BYTES + 1
            if length < 0 or length > MAX_BODY_BYTES:
                self._send(413, {"ok": False, "error": "payload_too_large"})
                return
            body = self.rfile.read(length)
            headers = {key.lower(): value for key, value in self.headers.items()}
            status, payload = application.handle("POST", self.path, headers, body)
            self._send(status, payload)

        def log_message(self, _format: str, *_args: Any) -> None:
            return

    return Handler


def serve(config: DraftServiceConfig | None = None) -> None:
    selected = config or DraftServiceConfig.from_env()
    application = DraftApplication(selected)
    server = HTTPServer((selected.host, selected.port), make_handler(application))
    server.serve_forever()
