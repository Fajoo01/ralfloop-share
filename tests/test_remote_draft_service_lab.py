import json
import struct

import pytest

from ralfloop_agent.inference_lab.draft_service import DraftApplication, DraftServiceConfig
from ralfloop_agent.inference_lab.gguf_tokenizer import GGUFTokenizer, read_gguf_metadata
from ralfloop_agent.inference_lab.remote_draft_protocol_v1 import (
    canonical_json,
    parse_draft_request,
    sign_body,
    validate_token_ids,
    verify_signature,
)


def _string(value: str) -> bytes:
    raw = value.encode()
    return struct.pack("<Q", len(raw)) + raw


def _value(value):
    if isinstance(value, str):
        return struct.pack("<I", 8) + _string(value)
    if isinstance(value, bool):
        return struct.pack("<I?", 7, value)
    if isinstance(value, int):
        return struct.pack("<II", 4, value)
    if isinstance(value, list):
        return struct.pack("<IIQ", 9, 8, len(value)) + b"".join(_string(item) for item in value)
    raise TypeError(value)


def _gguf(path):
    metadata = {
        "tokenizer.ggml.model": "gpt2",
        "tokenizer.ggml.pre": "qwen2",
        "tokenizer.ggml.tokens": ["a", "b", "ab", "<|im_start|>", "<|im_end|>"],
        "tokenizer.ggml.merges": ["a b"],
        "tokenizer.ggml.bos_token_id": 3,
        "tokenizer.ggml.eos_token_id": 4,
        "tokenizer.ggml.add_bos_token": False,
    }
    raw = b"GGUF" + struct.pack("<IQQ", 3, 0, len(metadata))
    raw += b"".join(_string(key) + _value(value) for key, value in metadata.items())
    path.write_bytes(raw)


class FakeBackend:
    def draft(self, prompt_token_ids, maximum):
        return [1] * maximum, {"queue_ms": 1.0, "inference_ms": 2.0}

    def tool(self, task, payload, maximum_bytes):
        return {"facts": [payload.get("goal", "")]}, {"queue_ms": 0.0, "inference_ms": 1.0}


def _application(tmp_path):
    path = tmp_path / "mini.gguf"
    _gguf(path)
    tokenizer = GGUFTokenizer(read_gguf_metadata(path))
    config = DraftServiceConfig("127.0.0.1", 19092, "qwen2.5:0.5b", str(path), "http://127.0.0.1:11434", b"x" * 32)
    return DraftApplication(config, tokenizer=tokenizer, backend=FakeBackend())


def _signed(app, payload):
    body = canonical_json(payload)
    timestamp = "1000"
    headers = {"x-ralf-timestamp": timestamp, "x-ralf-signature": sign_body(app.config.hmac_secret, timestamp, body)}
    return headers, body


def test_gguf_metadata_and_identity(tmp_path):
    path = tmp_path / "tiny.gguf"
    _gguf(path)
    tokenizer = GGUFTokenizer.from_file(path)
    assert tokenizer.identity.vocabulary_size == 5
    assert tokenizer.identity.tokenizer_hash
    assert tokenizer.decode(tokenizer.encode("ab")) == "ab"


def test_hmac_valid_and_replay_rejected():
    body = b"{}"
    signature = sign_body(b"x" * 32, "1000", body)
    verify_signature(b"x" * 32, "1000", body, signature, now=1000)
    with pytest.raises(ValueError, match="expired"):
        verify_signature(b"x" * 32, "1000", body, signature, now=2000)


def test_protocol_bounds_and_token_validation():
    with pytest.raises(ValueError):
        validate_token_ids([True])
    with pytest.raises(ValueError):
        parse_draft_request(b"{}", fixed_model="qwen2.5:0.5b")


def test_draft_application_requires_hmac(tmp_path, monkeypatch):
    app = _application(tmp_path)
    payload = {
        "protocol_version": "ralf-draft-v1",
        "request_id": "request_123",
        "model": "qwen2.5:0.5b",
        "prompt_token_ids": [0],
        "max_draft_tokens": 2,
        "temperature": 0,
    }
    headers, body = _signed(app, payload)
    monkeypatch.setattr("time.time", lambda: 1000)
    status, response = app.handle("POST", "/v1/draft", headers, body)
    assert status == 200
    assert response["draft_token_ids"] == [1, 1]
    status, response = app.handle("POST", "/v1/draft", {}, body)
    assert status == 401
    assert response["error"] == "invalid_hmac_timestamp"


def test_health_discloses_no_secret(tmp_path):
    app = _application(tmp_path)
    status, response = app.handle("GET", "/health", {}, b"")
    assert status == 200
    assert "secret" not in json.dumps(response).lower()
