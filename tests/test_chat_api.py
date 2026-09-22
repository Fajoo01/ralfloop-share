from __future__ import annotations

from contextlib import nullcontext

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import requests

from openshell_backend import chat_api
from ralfloop_agent.model_tools import ModelToolManager, ModelToolRegistry, ResourceDecision
from ralfloop_agent.providers.chat import (
    ChatChunk,
    ChatConnectTimeout,
    ChatInactivityTimeout,
    ChatInvalidResponse,
    ChatProviderConfigurationError,
    ChatProviderHTTPError,
    ChatProviderSettings,
    ChatResult,
    FallbackChatProvider,
    OllamaChatProvider,
    OpenAICompatibleChatProvider,
    build_chat_provider,
)
from ralfloop_agent.providers.llama_cpp import LlamaCppChatProvider


RUNTIME_TRUNCATED_DECISION = (
    '{"action":"tool","response":null,"tool_id":"deep_web_research_agentcpm_v1",'
    '"arguments":{"query":"quantizzazione quantistica o quantizzazione numerica in '
    'informatica e fisica?","domains":[],"max_sources":5,"max_steps":3,'
    '"seed_urls":[],"query":"quantizzazione"}'
)


class FakeResponse:
    def __init__(
        self,
        *,
        json_data: Any = None,
        lines: list[Any] | None = None,
        status_code: int = 200,
    ) -> None:
        self.json_data = json_data
        self.lines = list(lines or [])
        self.status_code = status_code
        self.closed = False
        self.iter_lines_calls: list[dict[str, Any]] = []

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        if isinstance(self.json_data, BaseException):
            raise self.json_data
        return self.json_data

    def iter_lines(self, chunk_size: int = 512, decode_unicode: bool = False):
        self.iter_lines_calls.append({"chunk_size": chunk_size, "decode_unicode": decode_unicode})
        for line in self.lines:
            if isinstance(line, BaseException):
                raise line
            yield line

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse | None = None, exc: Exception | None = None) -> None:
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        assert self.response is not None
        return self.response


class FakeProvider:
    name = "fake_local"
    default_model = "fake-model"

    def __init__(
        self,
        *,
        result: ChatResult | None = None,
        chunks: list[ChatChunk] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.result = result or ChatResult("ok", "fake-model", self.name)
        self.chunks = chunks or [ChatChunk("ok", model="fake-model"), ChatChunk(done=True, model="fake-model")]
        self.exc = exc
        self.chat_calls: list[tuple[list[dict[str, str]], str | None]] = []
        self.stream_calls: list[tuple[list[dict[str, str]], str | None]] = []

    def chat(self, messages, *, model=None):
        self.chat_calls.append((list(messages), model))
        if self.exc:
            raise self.exc
        return self.result

    def stream_chat(self, messages, *, model=None):
        self.stream_calls.append((list(messages), model))
        if self.exc:
            raise self.exc
        yield from self.chunks


def _client(provider: FakeProvider, manager: ModelToolManager | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(chat_api.router)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider
    if manager is not None:
        app.dependency_overrides[chat_api.get_model_tool_manager] = lambda: manager
    return TestClient(app)


def _events(response) -> list[dict[str, Any]]:
    return [json.loads(line) for line in response.text.splitlines() if line]


def test_chat_schema_and_non_streaming_response() -> None:
    provider = FakeProvider(
        result=ChatResult(
            text="risposta",
            model="configured-model",
            provider="fake_local",
            metadata={"eval_count": 3},
        )
    )
    response = _client(provider).post(
        "/chat",
        json={
            "message": "Fammi il punto",
            "history": [{"role": "assistant", "content": "Contesto precedente"}],
            "cwd": "/tmp/repo",
            "session_id": "session-1",
            "repo_context": {"branch": "main", "status": "clean"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "risposta"
    assert payload["model"] == "configured-model"
    assert payload["session_id"] == "session-1"
    assert payload["metadata"]["eval_count"] == 3
    assert payload["metadata"]["tools_executed"] is False
    assert payload["metadata"]["command_count"] == 0
    assert payload["metadata"]["result_ids"] == []
    messages, model = provider.chat_calls[0]
    assert model is None
    assert messages[-1] == {"role": "user", "content": "Fammi il punto"}
    assert "no tools" in messages[0]["content"]
    assert '"branch": "main"' in messages[0]["content"]


def test_orchestrated_chat_returns_natural_answer_and_no_fake_tool_provenance() -> None:
    provider = FakeProvider(
        result=ChatResult(
            text="Risposta naturale",
            model="configured-model",
            provider="fake_local",
            metadata={"endpoint": "http://local"},
        )
    )
    manager = ModelToolManager(ModelToolRegistry([]))

    response = _client(provider, manager).post(
        "/orchestrate",
        json={"message": "Come stai?", "session_id": "telegram-1"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "Risposta naturale"
    assert payload["metadata"]["interaction_mode"] == "orchestrated_chat"
    assert payload["metadata"]["tools_executed"] is False
    assert payload["metadata"]["command_count"] == 0


def test_orchestrate_dispatches_capability_action_and_returns_tool_results(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(Path("config/model_tools.json"), cache_root=tmp_path)
    spec = registry.get("deep_web_research_agentcpm_v1")
    snapshot = tmp_path / "models--openbmb--AgentCPM-Explore-GGUF" / "snapshots" / spec.revision
    snapshot.mkdir(parents=True)
    (snapshot / "AgentCPM-Explore.Q4_K_M.gguf").write_bytes(b"fixture")
    provider = FakeProvider(
        result=ChatResult(
            text=RUNTIME_TRUNCATED_DECISION,
            model="qwen-test",
            provider="llama_cpp",
            metadata={"endpoint": "http://127.0.0.1:19091"},
        )
    )
    calls = []
    output = {
        "run_id": "run-endpoint-fixture",
        "answer": "Risposta fixture.",
        "claims": [{"text": "Claim fixture.", "citation_ids": ["S1"]}],
        "citations": [{"source_id": "S1", "url": "https://example.org/source"}],
        "sources": [{"source_id": "S1", "url": "https://example.org/source"}],
        "steps": 2,
        "partial": False,
        "errors": [],
        "network_mode": "read_only",
        "trace_path": "/state/run-endpoint-fixture.jsonl",
    }

    def runner(selected, selected_snapshot, payload):
        calls.append(payload)
        return output

    manager = ModelToolManager(
        registry,
        runner=runner,
        resource_probe=lambda selected: ResourceDecision(ok=True, reason="fixture"),
        gpu_session_factory=lambda selected, tool_id: nullcontext({"enabled": False}),
    )

    request_message = (
        "Research atlas compiler allocation runtime limits, benefits, and methods."
    )
    response = _client(provider, manager).post(
        "/orchestrate", json={"message": request_message}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == (
        "Risposta fixture.\n\nAnalisi:\n- Claim fixture. [S1]\n\n"
        "Fonti:\n- [S1] https://example.org/source"
    )
    assert '"action"' not in payload["response"]
    assert len(calls) == 1
    assert calls[0]["max_steps"] == 10
    assert calls[0]["max_sources"] == 8
    assert calls[0]["query"] == request_message
    assert len(provider.chat_calls) == 1
    tool_output = payload["metadata"]["tool_results"][0]["output"]
    assert tool_output["run_id"] == "run-endpoint-fixture"
    assert tool_output["steps"] == 2
    assert tool_output["claims"] == output["claims"]
    assert tool_output["citations"] == output["citations"]


def test_chat_automatically_routes_explicit_deep_research_through_qwen_and_tool(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(Path("config/model_tools.json"), cache_root=tmp_path)
    spec = registry.get("deep_web_research_agentcpm_v1")
    snapshot = tmp_path / "models--openbmb--AgentCPM-Explore-GGUF" / "snapshots" / spec.revision
    snapshot.mkdir(parents=True)
    (snapshot / "AgentCPM-Explore.Q4_K_M.gguf").write_bytes(b"fixture")

    class SequenceProvider(FakeProvider):
        def __init__(self):
            super().__init__()
            self.responses = [
                '{"action":"tool","response":null,"tool_id":"deep_web_research_agentcpm_v1","arguments":{"query":"fonti","network_mode":"read_only"}}',
                "Ricerca completata con fonti.",
            ]

        def chat(self, messages, *, model=None):
            self.chat_calls.append((list(messages), model))
            return ChatResult(self.responses.pop(0), "qwen-test", "llama_cpp", {"endpoint": "http://127.0.0.1:19091"})

    output = {
        "run_id": "run-1",
        "answer": "risultato",
        "claims": [{"text": "fatto", "citation_ids": ["S1"]}],
        "citations": [{"source_id": "S1", "url": "https://example.org"}],
        "sources": [{"source_id": "S1", "url": "https://example.org"}],
        "steps": 2,
        "partial": False,
        "errors": [],
        "network_mode": "read_only",
        "trace_path": "/state/trace.jsonl",
    }
    manager = ModelToolManager(
        registry,
        runner=lambda selected, selected_snapshot, payload: output,
        resource_probe=lambda selected: ResourceDecision(ok=True, reason="fixture"),
        gpu_session_factory=lambda selected, tool_id: nullcontext({"enabled": False}),
    )
    response = _client(SequenceProvider(), manager).post(
        "/chat", json={"message": "Fai una ricerca web approfondita con fonti"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["response"] == "risultato\n\nAnalisi:\n- fatto [S1]\n\nFonti:\n- [S1] https://example.org"
    assert payload["metadata"]["automatic_tool_route"] is True
    assert payload["metadata"]["capability"] == "deep_web_research"
    assert payload["metadata"]["tools_executed"] is True
    assert payload["metadata"]["command_count"] == 1
    assert payload["metadata"]["final_render"] == "deterministic_tool_answer"


def test_chat_deep_research_unavailable_is_explicit_and_never_falls_back(tmp_path: Path) -> None:
    registry = ModelToolRegistry.load(Path("config/model_tools.json"), cache_root=tmp_path)
    manager = ModelToolManager(registry)
    provider = FakeProvider()

    response = _client(provider, manager).post(
        "/chat", json={"message": "Fai una ricerca web approfondita con fonti"}
    )

    assert response.status_code == 200
    payload = response.json()
    assert "snapshot_missing" in payload["response"]
    assert payload["metadata"]["tool_decision"] == "tool_unavailable"
    assert payload["metadata"]["tools_executed"] is False
    assert provider.chat_calls == []


def test_chat_stream_forwards_token_chunks_and_done() -> None:
    provider = FakeProvider(
        chunks=[
            ChatChunk(text="RALF_", model="m1"),
            ChatChunk(text="OK", model="m1"),
            ChatChunk(done=True, model="m1", metadata={"eval_count": 2}),
        ]
    )
    response = _client(provider).post(
        "/chat/stream",
        json={"message": "test", "session_id": "s1"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = _events(response)
    assert [event["type"] for event in events] == ["start", "token", "token", "done"]
    assert [event["text"] for event in events if event["type"] == "token"] == ["RALF_", "OK"]
    assert events[-1]["ok"] is True
    assert events[-1]["metadata"]["eval_count"] == 2
    assert events[-1]["metadata"]["tools_executed"] is False
    assert events[-1]["metadata"]["command_count"] == 0
    assert events[-1]["metadata"]["result_ids"] == []
    assert not any(event["type"] == "approval" for event in events)


def test_chat_stream_emits_error_and_failed_done() -> None:
    provider = FakeProvider(exc=ChatInactivityTimeout("idle"))
    response = _client(provider).post("/chat/stream", json={"message": "test"})

    events = _events(response)
    assert [event["type"] for event in events] == ["start", "error", "done"]
    assert events[1]["message"] == "provider_inactivity_timeout"
    assert events[-1]["ok"] is False


def test_chat_non_streaming_maps_provider_timeout() -> None:
    provider = FakeProvider(exc=ChatInactivityTimeout("idle"))
    response = _client(provider).post("/chat", json={"message": "test"})

    assert response.status_code == 504
    assert response.json()["detail"] == "provider_inactivity_timeout"


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        ({"message": "test", "cwd": "relative/path"}, "cwd"),
        ({"message": "test", "history": [{"role": "system", "content": "x"}]}, "history"),
        ({"message": "   "}, "message"),
    ],
)
def test_chat_schema_rejects_invalid_input(payload: dict[str, Any], field: str) -> None:
    response = _client(FakeProvider()).post("/chat", json=payload)

    assert response.status_code == 422
    assert field in response.text


def test_repo_context_has_hard_byte_limit() -> None:
    response = _client(FakeProvider()).post(
        "/chat",
        json={"message": "test", "repo_context": "x" * (chat_api.MAX_REPO_CONTEXT_BYTES + 1)},
    )

    assert response.status_code == 422
    assert "repo_context exceeds byte limit" in response.text


def test_history_is_bounded_from_most_recent_turns() -> None:
    request = chat_api.ChatRequest(
        message="now",
        history=[
            chat_api.ChatHistoryMessage(role="user", content="old"),
            chat_api.ChatHistoryMessage(role="assistant", content="newest"),
        ],
    )

    messages = chat_api.build_chat_messages(request, max_history_chars=4)

    assert messages[1:-1] == [
        {"role": "user", "content": "ld"},
        {"role": "assistant", "content": "st"},
    ]
    assert messages[-1]["content"] == "now"


def test_orphan_history_messages_are_not_sent() -> None:
    request = chat_api.ChatRequest(
        message="now",
        history=[
            chat_api.ChatHistoryMessage(role="assistant", content="orphan"),
            chat_api.ChatHistoryMessage(role="user", content="paired-user"),
            chat_api.ChatHistoryMessage(role="assistant", content="paired-assistant"),
            chat_api.ChatHistoryMessage(role="user", content="trailing"),
        ],
    )

    messages = chat_api.build_chat_messages(request)

    assert messages[1:-1] == [
        {"role": "user", "content": "paired-user"},
        {"role": "assistant", "content": "paired-assistant"},
    ]


def test_provider_configuration_failure_is_structured_for_both_endpoints() -> None:
    failure = ChatProviderConfigurationError("invalid_runtime_config")
    client = _client(failure)  # type: ignore[arg-type]

    response = client.post("/chat", json={"message": "test"})
    stream_response = client.post("/chat/stream", json={"message": "test", "session_id": "s1"})

    assert response.status_code == 500
    assert response.json()["detail"] == "provider_configuration_error"
    events = _events(stream_response)
    assert [event["type"] for event in events] == ["start", "error", "done"]
    assert events[1]["message"] == "provider_configuration_error"
    assert events[-1]["ok"] is False


def test_stream_close_propagates_to_provider_generator() -> None:
    class ClosingProvider(FakeProvider):
        def __init__(self) -> None:
            super().__init__()
            self.closed = False

        def stream_chat(self, messages, *, model=None):
            try:
                yield ChatChunk(text="one", model=self.default_model)
                while True:
                    yield ChatChunk(text="more", model=self.default_model)
            finally:
                self.closed = True

    provider = ClosingProvider()
    request = chat_api.ChatRequest(message="test")
    stream = chat_api._stream_events(request, provider, session_id="s1")

    assert json.loads(next(stream))["type"] == "start"
    assert json.loads(next(stream))["type"] == "token"
    stream.close()

    assert provider.closed is True


def test_ollama_non_streaming_uses_native_chat_endpoint() -> None:
    response = FakeResponse(
        json_data={
            "model": "configured-model",
            "message": {"role": "assistant", "content": "hello"},
            "done": True,
            "eval_count": 4,
        }
    )
    session = FakeSession(response)
    provider = OllamaChatProvider(
        base_url="http://ollama.local",
        model="configured-model",
        settings=ChatProviderSettings(1.25, 7.5),
        session=session,
    )

    result = provider.chat([{"role": "user", "content": "hi"}])

    assert result.text == "hello"
    assert result.metadata["eval_count"] == 4
    url, kwargs = session.calls[0]
    assert url == "http://ollama.local/api/chat"
    assert kwargs["json"]["stream"] is False
    assert kwargs["timeout"] == (1.25, 7.5)
    assert response.closed is True


def test_ollama_request_options_are_configurable_but_reserved_fields_are_protected() -> None:
    response = FakeResponse(
        json_data={
            "model": "configured-model",
            "message": {"role": "assistant", "content": "bounded"},
            "done": True,
        }
    )
    session = FakeSession(response)
    provider = OllamaChatProvider(
        base_url="http://ollama.local",
        model="configured-model",
        session=session,
        request_options={
            "options": {"temperature": 0, "num_predict": 128},
            "think": False,
            "keep_alive": 0,
            "model": "forbidden-override",
            "messages": [{"role": "system", "content": "forbidden"}],
            "stream": True,
        },
    )

    provider.chat([{"role": "user", "content": "hi"}])

    body = session.calls[0][1]["json"]
    assert body["model"] == "configured-model"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["stream"] is False
    assert body["options"] == {"temperature": 0, "num_predict": 128}
    assert body["think"] is False
    assert body["keep_alive"] == 0


def test_ollama_http_error_closes_response() -> None:
    response = FakeResponse(status_code=500)
    provider = OllamaChatProvider(base_url="http://ollama", model="m", session=FakeSession(response))

    with pytest.raises(ChatProviderHTTPError, match="provider_http_error"):
        provider.chat([{"role": "user", "content": "hi"}])
    assert response.closed is True


def test_ollama_stream_is_native_ndjson() -> None:
    response = FakeResponse(
        lines=[
            json.dumps({"model": "m", "message": {"content": "a"}, "done": False}),
            json.dumps({"model": "m", "message": {"content": "b"}, "done": False}),
            json.dumps({"model": "m", "message": {"content": ""}, "done": True, "eval_count": 2}),
        ]
    )
    session = FakeSession(response)
    provider = OllamaChatProvider(base_url="http://ollama", model="m", session=session)

    chunks = list(provider.stream_chat([{"role": "user", "content": "hi"}]))

    assert [chunk.text for chunk in chunks if chunk.text] == ["a", "b"]
    assert chunks[-1].done is True
    assert chunks[-1].metadata["eval_count"] == 2
    _, kwargs = session.calls[0]
    assert kwargs["stream"] is True
    assert kwargs["json"]["stream"] is True
    assert response.iter_lines_calls == [{"chunk_size": 1, "decode_unicode": True}]
    assert response.closed is True


def test_ollama_stream_rejects_invalid_ndjson() -> None:
    response = FakeResponse(lines=["not-json"])
    provider = OllamaChatProvider(
        base_url="http://ollama",
        model="m",
        session=FakeSession(response),
    )

    with pytest.raises(ChatInvalidResponse):
        list(provider.stream_chat([{"role": "user", "content": "hi"}]))
    assert response.closed is True


def test_ollama_has_separate_connect_and_inactivity_timeouts() -> None:
    connecting = OllamaChatProvider(
        base_url="http://ollama",
        model="m",
        session=FakeSession(exc=requests.ConnectTimeout("connect")),
    )
    with pytest.raises(ChatConnectTimeout):
        connecting.chat([{"role": "user", "content": "hi"}])

    response = FakeResponse(lines=[requests.ConnectionError("Read timed out.")])
    idle = OllamaChatProvider(
        base_url="http://ollama",
        model="m",
        session=FakeSession(response),
    )
    with pytest.raises(ChatInactivityTimeout):
        list(idle.stream_chat([{"role": "user", "content": "hi"}]))
    assert response.closed is True


def test_openai_compatible_stream_parses_sse() -> None:
    response = FakeResponse(
        lines=[
            'data: {"model":"local","choices":[{"delta":{"content":"A"},"finish_reason":null}]}',
            'data: {"model":"local","choices":[{"delta":{"content":"B"},"finish_reason":"stop"}]}',
            "data: [DONE]",
        ]
    )
    session = FakeSession(response)
    provider = OpenAICompatibleChatProvider(
        base_url="http://local-openai",
        model="local",
        session=session,
    )

    chunks = list(provider.stream_chat([{"role": "user", "content": "hi"}]))

    assert [chunk.text for chunk in chunks if chunk.text] == ["A", "B"]
    assert chunks[-1].done is True
    assert chunks[-1].metadata == {"finish_reason": "stop"}
    assert session.calls[0][0].endswith("/v1/chat/completions")
    assert response.iter_lines_calls == [{"chunk_size": 1, "decode_unicode": True}]
    assert response.closed is True


def test_provider_factory_uses_existing_runtime_config_and_planner_fallback(tmp_path: Path) -> None:
    config_path = tmp_path / "inference_runtime.json"
    config_path.write_text(
        json.dumps(
            {
                "default_runtime": "ollama",
                "runtimes": {
                    "ollama": {
                        "type": "ollama",
                        "base_url": "http://configured-ollama",
                        "models": {"planner": "configured-planner"},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    provider = build_chat_provider(config_path=config_path, session=FakeSession())

    assert isinstance(provider, OllamaChatProvider)
    assert provider.base_url == "http://configured-ollama"
    assert provider.default_model == "configured-planner"


def test_provider_factory_loads_chat_request_options(tmp_path: Path) -> None:
    config_path = tmp_path / "inference_runtime.json"
    config_path.write_text(
        json.dumps({
            "default_runtime": "ollama",
            "runtimes": {
                "ollama": {
                    "type": "ollama",
                    "base_url": "http://configured-ollama",
                    "models": {"chat": "configured-chat"},
                    "chat_request_options": {
                        "options": {"temperature": 0, "num_predict": 192},
                        "think": False,
                        "keep_alive": 0,
                    },
                }
            },
        }),
        encoding="utf-8",
    )

    provider = build_chat_provider(config_path=config_path, session=FakeSession())

    assert isinstance(provider, OllamaChatProvider)
    assert provider.request_options == {
        "options": {"temperature": 0, "num_predict": 192},
        "think": False,
        "keep_alive": 0,
    }


def test_provider_factory_selects_llama_cpp_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RALF_CHAT_PROVIDER", "llama_cpp")
    monkeypatch.setenv("RALF_LLAMA_CPP_MODEL", "qwen3.5:9b")
    monkeypatch.setenv("RALF_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19091")
    monkeypatch.setenv("RALF_LLAMA_CPP_FALLBACK", "none")

    provider = build_chat_provider(session=FakeSession())

    assert provider.name == "llama_cpp"
    assert provider.default_model == "qwen3.5:9b"
    assert provider.config.base_url == "http://127.0.0.1:19091"
    assert not isinstance(provider, OllamaChatProvider)


def test_provider_factory_without_provider_env_keeps_json_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RALF_CHAT_PROVIDER", raising=False)
    config_path = tmp_path / "inference_runtime.json"
    config_path.write_text(json.dumps({"default_runtime": "ollama", "runtimes": {"ollama": {
        "type": "ollama", "base_url": "http://json-ollama", "models": {"chat": "json-model"}
    }}}), encoding="utf-8")

    provider = build_chat_provider(config_path=config_path, session=FakeSession())

    assert isinstance(provider, OllamaChatProvider)
    assert provider.base_url == "http://json-ollama"
    assert provider.default_model == "json-model"


def test_provider_factory_preserves_configured_llama_cpp_ollama_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RALF_CHAT_PROVIDER", "llama_cpp")
    monkeypatch.setenv("RALF_LLAMA_CPP_MODEL", "qwen3.5:9b")
    monkeypatch.setenv("RALF_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19091")
    monkeypatch.setenv("RALF_LLAMA_CPP_FALLBACK", "ollama")
    config_path = tmp_path / "inference_runtime.json"
    config_path.write_text(json.dumps({"default_runtime": "ollama", "runtimes": {"ollama": {
        "type": "ollama", "base_url": "http://configured-fallback", "models": {"chat": "fallback-model"}
    }}}), encoding="utf-8")

    provider = build_chat_provider(config_path=config_path, session=FakeSession())

    assert isinstance(provider, FallbackChatProvider)
    assert provider.name == "llama_cpp"
    assert provider.default_model == "qwen3.5:9b"
    assert provider.configured_fallback == "ollama"
    assert isinstance(provider.primary, LlamaCppChatProvider)
    assert isinstance(provider.fallback, OllamaChatProvider)
    assert provider.fallback.base_url == "http://configured-fallback"
    assert provider.fallback.default_model == "fallback-model"


def test_chat_backend_has_no_agent_or_approval_dispatch() -> None:
    source = Path(chat_api.__file__).read_text(encoding="utf-8")
    forbidden = (
        "/tasks/run",
        "RalfloopAgent",
        "RecursiveMAS",
        "route_task",
        "sandbox_",
        "approve_confirmation",
        "execute_confirmed",
    )

    assert all(name not in source for name in forbidden)
