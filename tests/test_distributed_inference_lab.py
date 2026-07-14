from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from ralfloop_agent.inference_lab.benchmark_runner import BenchmarkPrompt, run_benchmark
from ralfloop_agent.inference_lab.config import InferenceLabConfig
from ralfloop_agent.inference_lab.manifest import build_manifest, write_manifest
from ralfloop_agent.inference_lab.provider import (
    FallbackChatProvider,
    RemoteToolChatProvider,
    SpeculativeLabProvider,
)
from ralfloop_agent.inference_lab.remote_mini_client import (
    CircuitBreaker,
    RemoteMiniClient,
    RemoteMiniInvalidResponse,
    RemoteMiniUnavailable,
)
from ralfloop_agent.inference_lab.remote_mini_protocol import (
    DraftRequest,
    DraftResponse,
    MiniLimits,
    MiniToolRequest,
    MiniToolResponse,
)
from ralfloop_agent.inference_lab.security import (
    LabSecurityError,
    redact_secrets,
    validate_local_endpoint,
    validate_remote_endpoint,
)
from ralfloop_agent.inference_lab.speculative_metrics import (
    SpeculativeMetrics,
    TokenizerMismatch,
    accepted_prefix,
    require_compatible_tokenizer,
)
from ralfloop_agent.inference_lab.vpn_benchmark import parse_ping_output, throughput_mbps
from ralfloop_agent.providers.chat import ChatChunk, ChatProviderError, ChatResult


class FakeTarget:
    name = "ollama"
    default_model = "target"

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.messages = None

    def chat(self, messages, *, model=None):
        self.messages = list(messages)
        if self.fail:
            raise ChatProviderError("failed")
        return ChatResult("ok", model or self.default_model, self.name, {"target": True})

    def stream_chat(self, messages, *, model=None):
        self.messages = list(messages)
        if self.fail:
            raise ChatProviderError("failed")
        yield ChatChunk(text="o", model=model or self.default_model)
        yield ChatChunk(text="k", model=model or self.default_model)
        yield ChatChunk(done=True, model=model or self.default_model, metadata={"target": True})


class FakeToolClient:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []

    def call(self, task, input_data):
        self.calls.append((task, input_data))
        if self.exc:
            raise self.exc
        return self.response


class FakeHTTPResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self.content = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.closed = False

    def iter_content(self, chunk_size=4096):
        yield self.content

    def close(self):
        self.closed = True


class FakeHTTPSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(kwargs)
        return response

    def get(self, url, **kwargs):
        return self.post(url, **kwargs)


def test_default_provider_and_experimental_flags_are_disabled(monkeypatch):
    for name in (
        "RALF_CHAT_PROVIDER",
        "RALF_REMOTE_MINI_ENABLED",
        "RALF_SPECULATIVE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    config = InferenceLabConfig.from_env()
    assert config.provider == "ollama"
    assert config.remote_mini_enabled is False
    assert config.speculative_enabled is False


def test_provider_and_draft_limits_are_validated():
    with pytest.raises(ValueError, match="unsupported_chat_provider"):
        InferenceLabConfig(provider="unknown")
    with pytest.raises(ValueError, match="invalid_speculative_max_draft_tokens"):
        InferenceLabConfig(speculative_max_draft_tokens=9)


def test_endpoint_allowlists_and_loopback_rules():
    assert validate_local_endpoint("http://127.0.0.1:8080")
    with pytest.raises(LabSecurityError, match="not_loopback"):
        validate_local_endpoint("http://10.252.14.12:8080")
    assert validate_remote_endpoint("http://10.252.14.12:9000", ("10.252.14.12",))
    with pytest.raises(LabSecurityError, match="not_allowlisted"):
        validate_remote_endpoint("http://10.252.14.12:9000", ("10.252.14.11",))
    with pytest.raises(LabSecurityError, match="credentials"):
        validate_remote_endpoint("http://user:pass@10.252.14.12:9000", ("10.252.14.12",))


def test_tool_protocol_bounds_secrets_and_actions():
    request = MiniToolRequest(task="repo_context_selection", input={"question": "where"})
    assert request.role == "tool"
    with pytest.raises(ValueError, match="remote_input_too_large"):
        MiniToolRequest(
            task="fact_extraction",
            input={"text": "x" * 300},
            limits=MiniLimits(max_input_bytes=256),
        )
    with pytest.raises(ValueError, match="secret_field_forbidden"):
        MiniToolRequest(task="fact_extraction", input={"api_token": "x"})
    with pytest.raises(ValueError, match="secret_value_forbidden"):
        MiniToolRequest(task="fact_extraction", input={"text": "Bearer abc.def"})
    with pytest.raises(ValueError, match="binding_action"):
        MiniToolResponse(
            request_id="request-123",
            ok=True,
            result={"command": "rm -rf /"},
        )
    with pytest.raises(ValueError, match="protected_action"):
        MiniToolResponse(
            request_id="request-123",
            ok=True,
            result={"proposal": "auto-approve now"},
        )


def test_draft_protocol_is_token_based_and_separate():
    request = DraftRequest(
        context_token_ids=[1, 2],
        tokenizer_hash="a" * 64,
        vocabulary_hash="b" * 64,
    )
    response = DraftResponse(
        request_id=request.request_id,
        ok=True,
        draft_token_ids=[3, 4],
        tokenizer_hash="a" * 64,
        vocabulary_hash="b" * 64,
    )
    assert response.protocol_version == "ralf-draft-v1"
    assert "text" not in type(response).model_fields


def test_tokenizer_and_vocabulary_mismatch_are_blocked():
    require_compatible_tokenizer(
        target_tokenizer_hash="a",
        draft_tokenizer_hash="a",
        target_vocabulary_hash="b",
        draft_vocabulary_hash="b",
    )
    with pytest.raises(TokenizerMismatch, match="tokenizer_mismatch"):
        require_compatible_tokenizer(
            target_tokenizer_hash="a",
            draft_tokenizer_hash="x",
            target_vocabulary_hash="b",
            draft_vocabulary_hash="b",
        )
    with pytest.raises(TokenizerMismatch, match="vocabulary_mismatch"):
        require_compatible_tokenizer(
            target_tokenizer_hash="a",
            draft_tokenizer_hash="a",
            target_vocabulary_hash="b",
            draft_vocabulary_hash="x",
        )


def test_draft_rejection_partial_acceptance_and_metrics():
    assert accepted_prefix([1, 2], [9, 2]) == 0
    assert accepted_prefix([1, 2, 3], [1, 2, 9]) == 2
    metrics = SpeculativeMetrics()
    assert metrics.record([1, 2, 3], [1, 2, 9], network_bytes=100, network_ms=6) == 2
    assert metrics.acceptance_rate == pytest.approx(2 / 3)
    assert metrics.to_dict()["network_bytes"] == 100


def test_circuit_breaker_opens_and_recovers():
    breaker = CircuitBreaker(failure_threshold=2, cooldown_sec=10)
    breaker.failure(now=1)
    assert breaker.allow(now=2) is True
    breaker.failure(now=2)
    assert breaker.allow(now=3) is False
    assert breaker.allow(now=12) is True


def test_remote_client_schema_timeout_retry_and_output_bound():
    def valid_response(kwargs):
        return FakeHTTPResponse(
            {
                "protocol_version": "ralf-mini-v1",
                "request_id": kwargs["json"]["request_id"],
                "ok": True,
                "result": {"facts": []},
                "timings": {},
            }
        )

    session = FakeHTTPSession([requests.Timeout(), valid_response])
    client = RemoteMiniClient(
        base_url="http://10.252.14.12:9000",
        allowed_hosts=("10.252.14.12",),
        session=session,
    )
    response = client.call("repo_context_selection", {"question": "q"})
    assert response.ok is True
    assert len(session.calls) == 2

    too_large = FakeHTTPSession([FakeHTTPResponse(b"x" * 9000)])
    client = RemoteMiniClient(
        base_url="http://10.252.14.12:9000",
        allowed_hosts=("10.252.14.12",),
        session=too_large,
    )
    with pytest.raises(RemoteMiniInvalidResponse, match="output_too_large"):
        client.call("repo_context_selection", {"question": "q"})


def test_remote_tool_disabled_and_failure_fall_back_locally():
    target = FakeTarget()
    provider = RemoteToolChatProvider(target=target, client=None)
    result = provider.chat([{"role": "user", "content": "q"}])
    assert result.text == "ok"
    assert result.metadata["remote_tool_used"] is False

    provider = RemoteToolChatProvider(
        target=target,
        client=FakeToolClient(exc=RemoteMiniUnavailable("down")),
    )
    assert provider.chat([{"role": "user", "content": "q"}]).text == "ok"


def test_remote_tool_is_untrusted_bounded_context_not_final_author():
    response = MiniToolResponse(
        request_id="request-123",
        ok=True,
        result={"facts": ["verified"], "files": ["README.md"], "confidence": 0.8},
    )
    target = FakeTarget()
    client = FakeToolClient(response=response)
    result = RemoteToolChatProvider(target=target, client=client).chat(
        [{"role": "system", "content": "safe"}, {"role": "user", "content": "q"}]
    )
    assert result.provider == "remote_tool"
    assert result.metadata["target_provider"] == "ollama"
    assert client.calls == [("repo_context_selection", {"question": "q"})]
    assert "non-binding" in target.messages[-2]["content"]


def test_local_engine_fallback_and_remote_speculation_truthfulness():
    fallback = FallbackChatProvider(name="llama_cpp", primary=FakeTarget(fail=True), fallback=FakeTarget())
    result = fallback.chat([{"role": "user", "content": "q"}])
    assert result.provider == "ollama"
    assert result.metadata["fallback_from"] == "llama_cpp"

    remote = SpeculativeLabProvider(name="speculative_remote", target=FakeTarget(), active=False, mode="remote")
    result = remote.chat([{"role": "user", "content": "q"}])
    assert result.metadata["speculative_active"] is False
    assert result.metadata["speculative_fallback"] == "target_autoregressive"

    local = SpeculativeLabProvider(name="speculative_local", target=fallback, active=True, mode="local")
    result = local.chat([{"role": "user", "content": "q"}])
    assert result.metadata["speculative_active"] is False
    assert result.metadata["reason"] == "target_provider_fallback"


def test_network_metrics_and_manifest_redaction(tmp_path: Path):
    ping = """64 bytes: time=5.0 ms\n64 bytes: time=7.0 ms\n2 packets transmitted, 2 received, 0% packet loss\n"""
    metrics = parse_ping_output(ping)
    assert metrics.p50_ms == 5.0
    assert metrics.p95_ms == 7.0
    assert throughput_mbps(1_000_000, 1.0) == 8.0

    manifest = build_manifest(
        environment={"gpu": "RTX 2070", "token": "nope"},
        configuration={"provider": "ollama"},
        results={"ttft_ms": 30_000},
    )
    path = write_manifest(tmp_path / "manifest.json", manifest)
    saved = json.loads(path.read_text())
    assert saved["environment"]["token"] == "[REDACTED]"
    assert saved["created_at"].endswith("+00:00")
    assert len(saved["content_sha256"]) == 64
    assert redact_secrets({"password": "x"}) == {"password": "[REDACTED]"}
    assert redact_secrets({"note": "api_key=abc"}) == {"note": "[REDACTED]"}


def test_benchmark_manifest_shape_with_fake_provider():
    result = run_benchmark(
        lambda: FakeTarget(),
        [BenchmarkPrompt("p1", "ciao", "short")],
        warmups=1,
        measured_runs=2,
    )
    assert result["warmups"] == 1
    assert len(result["samples"]) == 2
    assert result["summary"]["error_rate"] == 0


def test_lab_source_never_dispatches_agent_or_protected_actions():
    source = (
        Path(__file__).parents[1] / "ralfloop_agent" / "inference_lab" / "provider.py"
    ).read_text(encoding="utf-8")
    assert "/tasks/run" not in source
    assert "execute-approved" not in source
    assert "auto_execute_protected_actions" not in source
