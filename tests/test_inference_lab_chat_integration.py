from __future__ import annotations

import io

from openshell_backend import chat_api
from ralfloop_agent.cli import terminal_chat as cli
from ralfloop_agent.cli.session_store import SessionStore
from ralfloop_agent.providers.chat import ChatChunk, ChatResult


class FakeProvider:
    name = "ollama"
    default_model = "m"

    def chat(self, messages, *, model=None):
        return ChatResult("ok", model or "m", self.name)

    def stream_chat(self, messages, *, model=None):
        yield ChatChunk(text="ok", model=model or "m")
        yield ChatChunk(done=True, model=model or "m")


class FakeClient:
    def __init__(self):
        self.calls = []
        self.base_url = "http://fake"

    def post_chat_stream(self, payload):
        self.calls.append(("stream", payload))
        return iter(
            [
                {"type": "start", "provider": payload.get("provider"), "model": "m", "session_id": "s"},
                {"type": "token", "text": "ok"},
                {"type": "done", "ok": True, "provider": payload.get("provider"), "model": "m"},
            ]
        )

    def post_chat(self, payload):
        self.calls.append(("chat", payload))
        return {"response": "ok", "provider": payload.get("provider"), "model": "m", "session_id": "s"}

    def post_task(self, payload):
        self.calls.append(("task", payload))
        return {"final_answer": "agent"}


def _args(*args):
    return cli.build_parser().parse_args(list(args))


def test_api_default_uses_configured_ollama_without_lab(monkeypatch):
    configured = FakeProvider()
    called = []
    monkeypatch.setattr(chat_api, "build_experimental_provider", lambda name: called.append(name))
    request = chat_api.ChatRequest(message="ciao")
    result = chat_api.chat(request, configured)
    assert result.provider == "ollama"
    assert called == []


def test_api_environment_provider_is_explicit_opt_in(monkeypatch):
    experimental = FakeProvider()
    experimental.name = "llama_cpp"
    monkeypatch.setenv("RALF_CHAT_PROVIDER", "llama_cpp")
    monkeypatch.setattr(chat_api, "build_experimental_provider", lambda name: experimental)
    chat_api.get_chat_provider.cache_clear()
    try:
        assert chat_api.get_chat_provider().name == "llama_cpp"
    finally:
        chat_api.get_chat_provider.cache_clear()


def test_api_invalid_environment_provider_is_configuration_error(monkeypatch):
    monkeypatch.setenv("RALF_CHAT_PROVIDER", "unknown")
    chat_api.get_chat_provider.cache_clear()
    try:
        assert chat_api.get_chat_provider().code == "provider_configuration_error"
    finally:
        chat_api.get_chat_provider.cache_clear()


def test_api_explicit_provider_is_opt_in(monkeypatch):
    configured = FakeProvider()
    experimental = FakeProvider()
    experimental.name = "llama_cpp"
    monkeypatch.setattr(chat_api, "build_experimental_provider", lambda name: experimental)
    request = chat_api.ChatRequest(message="ciao", provider="llama_cpp")
    result = chat_api.chat(request, configured)
    assert result.provider == "llama_cpp"


def test_cli_default_ollama_and_explicit_provider_payload(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("RALF_CHAT_PROVIDER", raising=False)
    assert cli.config_from_args(_args("ask", "q")).provider == "ollama"
    client = FakeClient()
    err = io.StringIO()
    rc = cli.run_ask(
        _args("ask", "--provider", "llama_cpp", "q"),
        client=client,
        store=SessionStore(tmp_path / "sessions"),
        out=io.StringIO(),
        err=err,
    )
    assert rc == 0
    assert client.calls[0][1]["provider"] == "llama_cpp"
    assert "EXPERIMENTAL PROVIDER" in err.getvalue()
    assert all(kind != "task" for kind, _ in client.calls)


def test_interactive_provider_command_and_warning(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inputs = iter(["/provider remote_tool", "/provider", "/exit"])
    out = io.StringIO()
    rc = cli.run_chat(
        _args("chat"),
        client=FakeClient(),
        store=SessionStore(tmp_path / "sessions"),
        input_func=lambda prompt: next(inputs),
        out=out,
        err=io.StringIO(),
    )
    assert rc == 0
    assert "provider=remote_tool" in out.getvalue()
    assert "EXPERIMENTAL PROVIDER: remote_tool" in out.getvalue()


def test_experimental_provider_is_blocked_for_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient()
    err = io.StringIO()
    rc = cli.run_agent(
        _args("agent", "--provider", "speculative_remote", "goal"),
        client=client,
        out=io.StringIO(),
        err=err,
    )
    assert rc == 2
    assert "not_allowed_for_agent" in err.getvalue()
    assert client.calls == []


def test_chat_api_and_cli_keep_tasks_run_separate():
    api_source = open(chat_api.__file__, encoding="utf-8").read()
    assert "/tasks/run" not in api_source
    payload = cli.build_chat_payload("q", cli.ChatSession(session_id="s", cwd="/tmp"), None, provider="remote_tool")
    assert payload["provider"] == "remote_tool"
    assert cli.TASK_ENDPOINT not in str(payload)
