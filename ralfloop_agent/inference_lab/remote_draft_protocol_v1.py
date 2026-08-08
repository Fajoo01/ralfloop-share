from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any


PROTOCOL_VERSION = "ralf-draft-v1"
MAX_BODY_BYTES = 131_072
MAX_CONTEXT_TOKENS = 32_768
MAX_DRAFT_TOKENS = 8
ALLOWED_ADVISORY_TASKS = {
    "intent_classification",
    "domain_candidate_generation",
    "fact_extraction",
    "rule_candidate_extraction",
    "contradiction_detection",
    "context_compression",
    "structured_critic",
}


class DraftProtocolError(ValueError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_body(secret: bytes, timestamp: str, body: bytes) -> str:
    return hmac.new(secret, timestamp.encode("ascii") + b"." + body, hashlib.sha256).hexdigest()


def verify_signature(
    secret: bytes,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    now: float | None = None,
    max_skew_sec: int = 30,
) -> None:
    try:
        sent = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise DraftProtocolError("invalid_hmac_timestamp") from exc
    current = int(time.time() if now is None else now)
    if abs(current - sent) > max_skew_sec:
        raise DraftProtocolError("expired_hmac_timestamp")
    expected = sign_body(secret, timestamp, body)
    if not hmac.compare_digest(expected, str(signature)):
        raise DraftProtocolError("invalid_hmac_signature")


def validate_request_id(value: Any) -> str:
    text = str(value or "")
    if not 8 <= len(text) <= 64 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for char in text):
        raise DraftProtocolError("invalid_request_id")
    return text


def validate_token_ids(value: Any, *, maximum: int = MAX_CONTEXT_TOKENS) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise DraftProtocolError("invalid_token_ids_length")
    tokens: list[int] = []
    for item in value:
        if not isinstance(item, int) or isinstance(item, bool) or item < 0:
            raise DraftProtocolError("invalid_token_id")
        tokens.append(item)
    return tokens


def parse_draft_request(raw: bytes, *, fixed_model: str) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise DraftProtocolError("payload_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DraftProtocolError("invalid_json") from exc
    if not isinstance(payload, dict):
        raise DraftProtocolError("request_must_be_object")
    allowed = {"protocol_version", "request_id", "model", "prompt_token_ids", "max_draft_tokens", "temperature"}
    if set(payload) - allowed:
        raise DraftProtocolError("unknown_request_field")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise DraftProtocolError("invalid_protocol_version")
    if payload.get("model") != fixed_model:
        raise DraftProtocolError("model_not_allowed")
    if payload.get("temperature") not in {0, 0.0}:
        raise DraftProtocolError("temperature_must_be_zero")
    maximum = payload.get("max_draft_tokens")
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 1 <= maximum <= MAX_DRAFT_TOKENS:
        raise DraftProtocolError("invalid_max_draft_tokens")
    return {
        "protocol_version": PROTOCOL_VERSION,
        "request_id": validate_request_id(payload.get("request_id")),
        "model": fixed_model,
        "prompt_token_ids": validate_token_ids(payload.get("prompt_token_ids")),
        "max_draft_tokens": maximum,
        "temperature": 0,
    }


def parse_tokenize_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise DraftProtocolError("payload_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DraftProtocolError("invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) - {"protocol_version", "request_id", "text"}:
        raise DraftProtocolError("invalid_tokenize_request")
    if payload.get("protocol_version") != PROTOCOL_VERSION:
        raise DraftProtocolError("invalid_protocol_version")
    text = payload.get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > 65_536:
        raise DraftProtocolError("invalid_tokenize_text")
    return {"protocol_version": PROTOCOL_VERSION, "request_id": validate_request_id(payload.get("request_id")), "text": text}


def parse_tool_request(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_BODY_BYTES:
        raise DraftProtocolError("payload_too_large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DraftProtocolError("invalid_json") from exc
    if not isinstance(payload, dict) or set(payload) - {"protocol_version", "request_id", "role", "task", "input", "max_output_bytes"}:
        raise DraftProtocolError("invalid_tool_request")
    if payload.get("protocol_version") != "ralf-mini-v1" or payload.get("role") != "tool":
        raise DraftProtocolError("invalid_tool_protocol")
    task = payload.get("task")
    if task not in ALLOWED_ADVISORY_TASKS:
        raise DraftProtocolError("unsupported_advisory_task")
    data = payload.get("input")
    if not isinstance(data, dict) or len(canonical_json(data)) > 16_384:
        raise DraftProtocolError("invalid_tool_input")
    forbidden = ("secret", "password", "authorization", "cookie", "private_key")
    if any(marker in str(key).lower() for key in data for marker in forbidden):
        raise DraftProtocolError("secret_field_forbidden")
    maximum = payload.get("max_output_bytes", 8192)
    if not isinstance(maximum, int) or isinstance(maximum, bool) or not 256 <= maximum <= 8192:
        raise DraftProtocolError("invalid_tool_output_limit")
    return {
        "protocol_version": "ralf-mini-v1",
        "request_id": validate_request_id(payload.get("request_id")),
        "role": "tool",
        "task": task,
        "input": data,
        "max_output_bytes": maximum,
    }
