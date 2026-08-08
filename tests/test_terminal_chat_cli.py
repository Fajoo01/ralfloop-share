from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import requests

from ralfloop_agent.cli import terminal_chat as cli
from ralfloop_agent.cli.session_store import SessionStore


class FakeClient:
    def __init__(
        self,
        *,
        stream_events=None,
        chat_response=None,
        task_response=None,
        stream_exc=None,
        chat_exc=None,
    ):
        self.stream_events = list(
            stream_events
            or [
                {"type": "start", "provider": "fake", "model": "fake-model", "session_id": "s"},
                {"type": "token", "text": "ok"},
                {"type": "done", "ok": True, "provider": "fake", "model": "fake-model"},
            ]
        )
        self.chat_response = chat_response or {
            "response": "ok",
            "provider": "fake",
            "model": "fake-model",
            "session_id": "s",
        }
        self.task_response = task_response or {"final_answer": "agent-ok"}
        self.stream_exc = stream_exc
        self.chat_exc = chat_exc
        self.calls = []
        self.base_url = "http://fake"

    def post_chat_stream(self, payload):
        self.calls.append(("stream", payload))
        if self.stream_exc:
            raise self.stream_exc
        return iter(self.stream_events)

    def post_chat(self, payload):
        self.calls.append(("chat", payload))
        if self.chat_exc:
            raise self.chat_exc
        return self.chat_response

    def post_task(self, payload):
        self.calls.append(("task", payload))
        return self.task_response

    def get_json(self, path):
        self.calls.append(("get", path))
        if path == "/openapi.json":
            return {"paths": {"/chat": {}, "/chat/stream": {}, "/tasks/run": {}}}
        if path == "/domain-approvals/health":
            return {"enabled": True, "auto_execute": False, "hmac_key_file": "/secret"}
        return {}


def _args(*argv):
    return cli.build_parser().parse_args(list(argv))


def _store(tmp_path: Path) -> SessionStore:
    return SessionStore(tmp_path / "sessions")


def test_extract_and_sanitize_response():
    assert cli.extract_text_response({"final_answer": "ok"}) == "ok"
    text = cli.extract_text_response({"x": {"token": "abc", "value": 1}})
    assert "[REDACTED]" in text
    assert "abc" not in text


def test_ask_streams_tokens_and_never_calls_tasks_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        stream_events=[
            {"type": "start", "provider": "ollama", "model": "local", "session_id": "s"},
            {"type": "token", "text": "RALF_"},
            {"type": "token", "text": "OK"},
            {"type": "done", "ok": True, "provider": "ollama", "model": "local"},
        ]
    )
    out = io.StringIO()

    rc = cli.run_ask(_args("ask", "hello"), client=client, store=_store(tmp_path), out=out, err=io.StringIO())

    assert rc == 0
    assert out.getvalue() == "RALF_OK"
    assert [kind for kind, _ in client.calls] == ["stream"]
    payload = client.calls[0][1]
    assert payload["cwd"] == str(tmp_path)
    assert isinstance(payload["repo_context"], dict)


def test_ask_naturally_escalates_read_only_request_to_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(task_response={"final_answer": "read-only-ok"})
    out = io.StringIO()
    err = io.StringIO()

    rc = cli.run_ask(
        _args("ask", "Controlla lo spazio e non cancellare nulla"),
        client=client,
        store=_store(tmp_path),
        out=out,
        err=err,
    )

    assert rc == 0
    assert [kind for kind, _ in client.calls] == ["task"]
    payload = client.calls[0][1]
    terminal = payload["extra_context"]["terminal_client"]
    assert terminal["interaction_mode"] == "agent"
    assert terminal["capability"] == "read_only_system_inspection"
    assert terminal["provider"] == "llama_cpp"
    assert terminal["provider_endpoint"] == "http://127.0.0.1:19091"
    assert "interaction_mode=agent" in err.getvalue()


def test_interactive_plain_read_only_message_uses_task_not_chat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(task_response={"final_answer": "read-only-ok"})
    inputs = iter(["Controlla lo spazio e non cancellare nulla", "/exit"])

    rc = cli.run_chat(
        _args("chat"),
        client=client,
        store=_store(tmp_path),
        input_func=lambda prompt: next(inputs),
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert rc == 0
    assert [kind for kind, _ in client.calls] == ["task"]


def test_mixed_natural_request_uses_protected_task_without_autoapproval(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        task_response={
            "final_answer": "human_confirmation_required",
            "pending_confirmation_id": "pending-1",
        }
    )

    rc = cli.run_ask(
        _args("ask", "Non cancellare i modelli, ma elimina i temporanei"),
        client=client,
        store=_store(tmp_path),
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert rc == 0
    payload = client.calls[0][1]
    assert payload["extra_context"]["terminal_client"]["capability"] == "protected_external_action"
    assert "human_confirmed" not in payload["extra_context"]


def test_stream_output_strips_terminal_escape_and_control_sequences(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        stream_events=[
            {"type": "start", "provider": "fake", "model": "m"},
            {"type": "token", "text": "safe\x1b]2;owned\x07text\x9b31m"},
            {"type": "done", "ok": True},
        ]
    )
    out = io.StringIO()

    rc = cli.run_ask(_args("ask", "hello"), client=client, store=_store(tmp_path), out=out, err=io.StringIO())

    assert rc == 0
    assert "\x1b" not in out.getvalue()
    assert "\x07" not in out.getvalue()
    assert "\x9b" not in out.getvalue()
    assert out.getvalue().startswith("safe")


def test_non_stream_output_strips_terminal_escape_sequences(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        chat_response={
            "response": "safe\x1b[31mred\x1b[0m",
            "provider": "p",
            "model": "m",
            "session_id": "s",
        }
    )
    out = io.StringIO()

    rc = cli.run_ask(
        _args("ask", "--no-stream", "hello"),
        client=client,
        store=_store(tmp_path),
        out=out,
        err=io.StringIO(),
    )

    assert rc == 0
    assert out.getvalue().strip() == "safered"


def test_stream_unavailable_falls_back_only_to_chat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        stream_exc=cli.StreamFastPathUnavailable("missing"),
        chat_response={"response": "fallback", "provider": "fake", "model": "m", "session_id": "s"},
    )
    out = io.StringIO()
    err = io.StringIO()

    rc = cli.run_ask(_args("ask", "hello"), client=client, store=_store(tmp_path), out=out, err=err)

    assert rc == 0
    assert out.getvalue().strip() == "fallback"
    assert [kind for kind, _ in client.calls] == ["stream", "chat"]
    assert "stream fast-path" in err.getvalue()


def test_stream_error_never_falls_back_or_calls_task(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(stream_exc=cli.RalfTerminalError("invalid_ndjson"))
    err = io.StringIO()

    rc = cli.run_ask(_args("ask", "hello"), client=client, store=_store(tmp_path), out=io.StringIO(), err=err)

    assert rc == 1
    assert [kind for kind, _ in client.calls] == ["stream"]
    assert "invalid_ndjson" in err.getvalue()


def test_stream_error_event_is_user_facing_and_turn_not_saved(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        stream_events=[
            {"type": "start", "provider": "fake", "model": "m"},
            {"type": "error", "message": "provider_inactivity_timeout"},
            {"type": "done", "ok": False},
        ]
    )
    store = _store(tmp_path)
    err = io.StringIO()

    rc = cli.run_ask(_args("ask", "hello"), client=client, store=store, out=io.StringIO(), err=err)

    assert rc == 1
    assert "provider_inactivity_timeout" in err.getvalue()
    assert store.latest()["history"] == []


def test_stream_error_event_cannot_be_overridden_by_successful_done(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        stream_events=[
            {"type": "start", "provider": "fake", "model": "m"},
            {"type": "token", "text": "partial"},
            {"type": "error", "message": "provider_error"},
            {"type": "done", "ok": True},
        ]
    )
    store = _store(tmp_path)

    rc = cli.run_ask(_args("ask", "hello"), client=client, store=store, out=io.StringIO(), err=io.StringIO())

    assert rc == 1
    assert store.latest()["history"] == []


def test_stream_error_strips_terminal_control_sequences(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        stream_events=[
            {"type": "start", "provider": "fake", "model": "m"},
            {"type": "error", "message": "failure\x1b]2;owned\x07"},
            {"type": "done", "ok": False},
        ]
    )
    err = io.StringIO()

    rc = cli.run_ask(_args("ask", "hello"), client=client, store=_store(tmp_path), out=io.StringIO(), err=err)

    assert rc == 1
    assert err.getvalue().splitlines()[-1] == "failure"
    assert "\x1b" not in err.getvalue()


def test_stream_approval_only_is_displayed_without_fake_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(
        stream_events=[
            {"type": "start", "provider": "fake", "model": "m"},
            {"type": "approval", "request_id": "apr-1", "action": "protected", "status": "pending"},
            {"type": "done", "ok": True},
        ]
    )
    store = _store(tmp_path)
    out = io.StringIO()

    rc = cli.run_ask(_args("ask", "hello"), client=client, store=store, out=out, err=io.StringIO())

    assert rc == 0
    assert "APPROVAZIONE RICHIESTA" in out.getvalue()
    assert store.latest()["history"] == []


def test_ask_json_uses_non_streaming_chat(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(chat_response={"response": "answer", "provider": "p", "model": "m", "session_id": "s"})
    out = io.StringIO()

    rc = cli.run_ask(_args("ask", "--json", "hello"), client=client, store=_store(tmp_path), out=out, err=io.StringIO())

    assert rc == 0
    assert json.loads(out.getvalue())["response"] == "answer"
    assert [kind for kind, _ in client.calls] == ["chat"]


def test_ask_raw_prints_complete_non_streaming_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(chat_response={"response": "answer", "provider": "p", "model": "m", "session_id": "s"})
    out = io.StringIO()

    rc = cli.run_ask(_args("ask", "--raw", "hello"), client=client, store=_store(tmp_path), out=out, err=io.StringIO())

    assert rc == 0
    assert json.loads(out.getvalue())["response"] == "answer"
    assert [kind for kind, _ in client.calls] == ["chat"]


def test_explicit_agent_requires_confirmation_and_does_not_auto_approve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient()
    out = io.StringIO()
    answers = iter(["n"])
    rc = cli.run_agent(
        _args("agent", "do", "work"),
        client=client,
        input_func=lambda prompt: next(answers),
        out=out,
        err=io.StringIO(),
    )
    assert rc == 2
    assert not client.calls

    answers = iter(["y"])
    rc = cli.run_agent(
        _args("agent", "do", "work"),
        client=client,
        input_func=lambda prompt: next(answers),
        out=out,
        err=io.StringIO(),
    )
    assert rc == 0
    assert [kind for kind, _ in client.calls] == ["task"]
    task_payload = client.calls[0][1]
    assert "human_confirmed" not in task_payload.get("extra_context", {})
    assert task_payload["extra_context"]["terminal_client"]["auto_execute_protected_actions"] is False


def test_agent_yes_is_only_workflow_confirmation(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(task_response={"pending_confirmation_id": "c1", "stop_reason": "human_confirmation_required"})
    out = io.StringIO()

    rc = cli.run_agent(_args("agent", "--yes", "goal"), client=client, out=out, err=io.StringIO())

    assert rc == 0
    assert [kind for kind, _ in client.calls] == ["task"]
    assert "APPROVAZIONE RICHIESTA" in out.getvalue()
    assert "Telegram" in out.getvalue()


def test_approval_is_recognized_without_execute_calls():
    payload = {
        "approval_request": {
            "request_id": "apr_1",
            "scope_digest_short": "ABCD-1234",
            "action": "run_domain_canary",
            "status": "pending",
        }
    }
    approval = cli.find_approval_request(payload)
    assert approval == {
        "request_id": "apr_1",
        "digest": "ABCD-1234",
        "action": "run_domain_canary",
        "status": "pending",
    }
    text = cli.format_approval_notice(approval)
    assert "approvo" in text
    assert "execute-approved" not in text


def test_session_resume_sends_bounded_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = _store(tmp_path)
    first = FakeClient()
    assert cli.run_ask(_args("ask", "first"), client=first, store=store, out=io.StringIO(), err=io.StringIO()) == 0
    session_id = store.latest()["session_id"]
    second = FakeClient()

    assert (
        cli.run_ask(
            _args("--session", session_id, "ask", "second"),
            client=second,
            store=store,
            out=io.StringIO(),
            err=io.StringIO(),
        )
        == 0
    )
    history = second.calls[0][1]["history"]
    assert history == [{"role": "user", "content": "first"}, {"role": "assistant", "content": "ok"}]


def test_history_is_bounded_by_turns_and_characters():
    session = cli.ChatSession(history_limit=2)
    session.add("old", "old-answer")
    session.add("new", "new-answer")
    session.add("latest", "latest-answer")
    assert [turn.user for turn in session.turns] == ["new", "latest"]
    assert sum(len(row["content"]) for row in session.messages(max_chars=100)) <= 100
    short = session.messages(max_chars=5)
    assert [row["role"] for row in short] == ["user", "assistant"]
    assert sum(len(row["content"]) for row in short) == 5


def test_invalid_cwd_is_user_facing(tmp_path):
    err = io.StringIO()
    rc = cli.run_ask(
        _args("ask", "--cwd", str(tmp_path / "missing"), "hello"),
        client=FakeClient(),
        store=_store(tmp_path),
        out=io.StringIO(),
        err=err,
    )
    assert rc == 2
    assert "invalid_cwd" in err.getvalue()


def test_interactive_ctrl_c_interrupts_generation_not_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class InterruptingClient(FakeClient):
        def post_chat_stream(self, payload):
            self.calls.append(("stream", payload))

            def events():
                yield {"type": "start", "provider": "fake", "model": "m"}
                raise KeyboardInterrupt

            return events()

    inputs = iter(["hello", "/exit"])
    out = io.StringIO()
    rc = cli.run_chat(
        _args("chat"),
        client=InterruptingClient(),
        store=_store(tmp_path),
        input_func=lambda prompt: next(inputs),
        out=out,
        err=io.StringIO(),
    )
    assert rc == 0
    assert "generazione interrotta" in out.getvalue()


def test_interactive_ctrl_c_interrupts_agent_not_session(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class InterruptingAgentClient(FakeClient):
        def post_task(self, payload):
            self.calls.append(("task", payload))
            raise KeyboardInterrupt

    inputs = iter(["/agent goal", "y", "/exit"])
    out = io.StringIO()

    rc = cli.run_chat(
        _args("chat"),
        client=InterruptingAgentClient(),
        store=_store(tmp_path),
        input_func=lambda prompt: next(inputs),
        out=out,
        err=io.StringIO(),
    )

    assert rc == 0
    assert "generazione agente interrotta" in out.getvalue()


def test_interactive_relative_cwd_and_failed_change_roll_back(tmp_path, monkeypatch):
    child = tmp_path / "child"
    child.mkdir()
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    monkeypatch.chdir(tmp_path)
    real_collect = cli.collect_repo_context

    def collect(path):
        if Path(path).resolve() == blocked.resolve():
            raise cli.RepoContextError("context_failed")
        return real_collect(path)

    monkeypatch.setattr(cli, "collect_repo_context", collect)
    inputs = iter(["/cwd child", "/cwd ../blocked", "/cwd", "/exit"])
    out = io.StringIO()
    store = _store(tmp_path)

    rc = cli.run_chat(
        _args("chat"),
        client=FakeClient(),
        store=store,
        input_func=lambda prompt: next(inputs),
        out=out,
        err=io.StringIO(),
    )

    assert rc == 0
    assert store.latest()["cwd"] == str(child)
    assert f"cwd={child}" in out.getvalue()


def test_noninteractive_agent_requires_yes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    class NonTTY:
        def isatty(self):
            return False

    monkeypatch.setattr(cli.sys, "stdin", NonTTY())
    client = FakeClient()
    err = io.StringIO()

    rc = cli.run_agent(_args("agent", "goal"), client=client, out=io.StringIO(), err=err)

    assert rc == 2
    assert client.calls == []
    assert "usa --yes" in err.getvalue()


def test_agent_without_message_starts_persistent_agentic_repl(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(task_response={"final_answer": "agent-ok"})
    store = _store(tmp_path)
    inputs = iter(["controlla lo spazio", "/history", "/exit"])
    out = io.StringIO()

    rc = cli.run_agent(
        _args("agent"),
        client=client,
        store=store,
        input_func=lambda prompt: next(inputs),
        out=out,
        err=io.StringIO(),
    )

    assert rc == 0
    assert [kind for kind, _ in client.calls] == ["task"]
    payload = client.calls[0][1]
    terminal = payload["extra_context"]["terminal_client"]
    assert terminal["provider"] == "llama_cpp"
    assert terminal["auto_execute_protected_actions"] is False
    assert "human_confirmed" not in payload["extra_context"]
    assert "Ralf Agent" in out.getvalue()
    assert "mode: every message uses /tasks/run" in out.getvalue()
    assert "agent-ok" in out.getvalue()
    assert "controlla lo spazio" in out.getvalue()
    assert store.latest()["history"] == [
        {"role": "user", "content": "controlla lo spazio"},
        {"role": "assistant", "content": "agent-ok"},
    ]


def test_agentic_repl_sends_every_plain_message_to_task_with_history(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient(task_response={"final_answer": "agent-ok"})
    inputs = iter(["primo", "secondo", "/exit"])

    rc = cli.run_agent(
        _args("agent"),
        client=client,
        store=_store(tmp_path),
        input_func=lambda prompt: next(inputs),
        out=io.StringIO(),
        err=io.StringIO(),
    )

    assert rc == 0
    assert [kind for kind, _ in client.calls] == ["task", "task"]
    second = client.calls[1][1]
    assert second["extra_context"]["terminal_client"]["conversation_history"] == [
        {"user": "primo", "ralf": "agent-ok"}
    ]
    assert "human_confirmed" not in second["extra_context"]


def test_status_is_read_only_and_reports_fast_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    client = FakeClient()
    inputs = iter(["/status", "/exit"])
    out = io.StringIO()

    assert (
        cli.run_chat(
            _args("chat"),
            client=client,
            store=_store(tmp_path),
            input_func=lambda prompt: next(inputs),
            out=out,
            err=io.StringIO(),
        )
        == 0
    )
    assert [kind for kind, _ in client.calls] == ["get", "get"]
    assert '"chat_stream_available": true' in out.getvalue()
    assert "[REDACTED]" in out.getvalue()


class TTYBuffer(io.StringIO):
    def isatty(self):
        return True


def test_no_color_is_respected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    out = TTYBuffer()
    assert (
        cli.run_chat(
            _args("chat"),
            client=FakeClient(),
            store=_store(tmp_path),
            input_func=lambda prompt: "/exit",
            out=out,
            err=io.StringIO(),
        )
        == 0
    )
    assert "\x1b[" not in out.getvalue()


def test_cli_repairs_known_utf8_mojibake() -> None:
    assert cli.sanitize_terminal_text("non c'Ã¨") == "non c'è"


def test_banner_prints_llama_cpp_provider_once(tmp_path) -> None:
    out = io.StringIO()
    cli._print_banner(
        cli.ChatSession(session_id="s", cwd=str(tmp_path)),
        cli.ChatConfig(provider="llama_cpp"),
        out,
    )
    assert out.getvalue().count("provider: llama_cpp") == 1


def test_backend_unreachable_and_inactivity_timeout_are_user_facing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for exc in (cli.BackendUnavailable("offline"), cli.BackendTimeout("stream_inactivity_timeout")):
        err = io.StringIO()
        rc = cli.run_ask(
            _args("ask", "hello"),
            client=FakeClient(stream_exc=exc),
            store=_store(tmp_path),
            out=io.StringIO(),
            err=err,
        )
        assert rc == 1
        assert str(exc) in err.getvalue()


class HTTPFakeResponse:
    def __init__(self, *, lines=None, json_data=None, status_code=200):
        self.lines = list(lines or [])
        self.json_data = json_data
        self.status_code = status_code
        self.text = "body"
        self.closed = False

    def iter_lines(self, **kwargs):
        yield from self.lines

    def json(self):
        if isinstance(self.json_data, Exception):
            raise self.json_data
        return self.json_data

    def close(self):
        self.closed = True


class HTTPFakeSession:
    def __init__(self, response):
        self.response = response

    def post(self, *args, **kwargs):
        return self.response

    def request(self, *args, **kwargs):
        return self.response


def test_http_client_rejects_invalid_ndjson_and_closes_response():
    response = HTTPFakeResponse(lines=["not-json"])
    client = cli.RalfHTTPClient("http://fake", session=HTTPFakeSession(response))
    with pytest.raises(cli.RalfTerminalError, match="invalid_ndjson"):
        list(client.post_chat_stream({"message": "x"}))
    assert response.closed is True


def test_http_client_ctrl_c_closes_stream_response():
    class InterruptingResponse(HTTPFakeResponse):
        def iter_lines(self, **kwargs):
            yield '{"type":"start"}'
            raise KeyboardInterrupt

    response = InterruptingResponse()
    client = cli.RalfHTTPClient("http://fake", session=HTTPFakeSession(response))

    with pytest.raises(KeyboardInterrupt):
        list(client.post_chat_stream({"message": "x"}))
    assert response.closed is True


def test_http_client_rejects_invalid_json():
    response = HTTPFakeResponse(json_data=ValueError("bad"))
    client = cli.RalfHTTPClient("http://fake", session=HTTPFakeSession(response))
    with pytest.raises(cli.RalfTerminalError, match="invalid_json"):
        client.post_chat({"message": "x"})
    assert response.closed is True


def test_parser_has_required_modes_and_preserves_global_flags():
    parser = cli.build_parser()
    assert parser.parse_args(["ask", "hello"]).command == "ask"
    assert parser.parse_args(["agent", "--yes", "goal"]).yes is True
    assert parser.parse_args(["sessions", "show", "abc"]).sessions_command == "show"
    args = parser.parse_args(["--base-url", "http://x", "chat"])
    assert args.base_url == "http://x"


def test_sessions_show_and_delete_commands(tmp_path):
    store = _store(tmp_path)
    record = store.create(cwd=str(tmp_path), model="m")
    session_id = record["session_id"]
    out = io.StringIO()

    assert cli.run_sessions(_args("sessions", "show", session_id), store=store, out=out, err=io.StringIO()) == 0
    assert json.loads(out.getvalue())["session_id"] == session_id

    out = io.StringIO()
    assert cli.run_sessions(_args("sessions", "delete", session_id), store=store, out=out, err=io.StringIO()) == 0
    assert f"deleted={session_id}" in out.getvalue()
    with pytest.raises(cli.SessionStoreError):
        store.load(session_id)
