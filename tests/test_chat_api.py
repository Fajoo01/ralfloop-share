from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import requests

from openshell_backend import chat_api
from ralfloop_agent.providers.chat import (
    ChatChunk,
    ChatConnectTimeout,
    ChatInactivityTimeout,
    ChatInvalidResponse,
    ChatProviderConfigurationError,
    ChatProviderHTTPError,
    ChatProviderSettings,
    ChatResult,
    OllamaChatProvider,
    OpenAICompatibleChatProvider,
    build_chat_provider,
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


def _client(provider: FakeProvider) -> TestClient:
    app = FastAPI()
    app.include_router(chat_api.router)
    app.dependency_overrides[chat_api.get_chat_provider] = lambda: provider
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
    assert payload["metadata"] == {"eval_count": 3}
    messages, model = provider.chat_calls[0]
    assert model is None
    assert messages[-1] == {"role": "user", "content": "Fammi il punto"}
    assert "no tools" in messages[0]["content"]
    assert '"branch": "main"' in messages[0]["content"]


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
    assert events[-1]["metadata"] == {"eval_count": 2}
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
