from __future__ import annotations

import argparse
import io
import json

import pytest

from ralfloop_agent.cli import terminal_chat as cli


class FakeClient:
    def __init__(self, responses=None, exc=None):
        self.responses = list(responses or [])
        self.exc = exc
        self.calls = []
        self.base_url = "http://fake"

    def post_task(self, payload):
        self.calls.append(payload)
        if self.exc:
            raise self.exc
        return self.responses.pop(0)

    def get_json(self, path):
        if path == "/openapi.json":
            return {"paths": {"/tasks/run": {}}}
        if path == "/domain-approvals/health":
            return {"enabled": True, "hmac_key_file": "/secret"}
        return {}


def test_extract_text_response_known_keys():
    assert cli.extract_text_response({"final_answer": "ok"}) == "ok"
    assert cli.extract_text_response({"result_envelope": {"answer": "nested"}}) == "nested"


def test_extract_text_response_fallback_json_redacts_secret():
    text = cli.extract_text_response({"x": {"token": "abc", "value": 1}})
    assert "[REDACTED]" in text
    assert "abc" not in text


def test_run_ask_prints_answer_only_by_default():
    out = io.StringIO()
    err = io.StringIO()
    args = argparse.Namespace(message=["ciao"], base_url=None, timeout=None, raw=False, json=False, no_history=False)
    rc = cli.run_ask(args, client=FakeClient([{"final_answer": "risposta"}]), out=out, err=err)
    assert rc == 0
    assert out.getvalue().strip() == "risposta"
    assert err.getvalue() == ""


def test_run_ask_raw_prints_json():
    out = io.StringIO()
    args = argparse.Namespace(message=["ciao"], base_url=None, timeout=None, raw=True, json=False, no_history=False)
    rc = cli.run_ask(args, client=FakeClient([{"final_answer": "risposta"}]), out=out)
    assert rc == 0
    assert json.loads(out.getvalue())["final_answer"] == "risposta"


def test_approval_notice_detected_without_execution():
    out = io.StringIO()
    payload = {"request": {"request_id": "apr_1", "scope_digest_short": "ABCD-1234", "action": "run_domain_canary", "status": "pending"}}
    rc = cli.emit_response(payload, out=out)
    text = out.getvalue()
    assert rc == 0
    assert "APPROVAZIONE RICHIESTA" in text
    assert "apr_1" in text
    assert "approvo" in text
    assert "execute-approved" not in text


def test_json_output_remains_valid_with_approval():
    out = io.StringIO()
    payload = {"request": {"request_id": "apr_1", "scope_digest_short": "ABCD-1234", "action": "run_domain_canary", "status": "pending"}}
    rc = cli.emit_response(payload, json_output=True, out=out)
    assert rc == 0
    data = json.loads(out.getvalue())
    assert data["terminal_approval"]["request_id"] == "apr_1"


def test_http_error_is_user_facing():
    out = io.StringIO()
    err = io.StringIO()
    args = argparse.Namespace(message=["x"], base_url=None, timeout=None, raw=False, json=False, no_history=False)
    rc = cli.run_ask(args, client=FakeClient(exc=cli.BackendHTTPError(500, "boom")), out=out, err=err)
    assert rc == 1
    assert "HTTP 500" in err.getvalue()


def test_timeout_and_unreachable_are_user_facing():
    args = argparse.Namespace(message=["x"], base_url=None, timeout=None, raw=False, json=False, no_history=False)
    for exc in (cli.BackendTimeout("backend_timeout"), cli.BackendUnavailable("backend_unreachable")):
        err = io.StringIO()
        assert cli.run_ask(args, client=FakeClient(exc=exc), out=io.StringIO(), err=err) == 1
        assert str(exc) in err.getvalue()


def test_history_limit_and_reset():
    session = cli.ChatSession(history_limit=2)
    session.add("u1", "r1")
    session.add("u2", "r2")
    session.add("u3", "r3")
    assert [turn.user for turn in session.turns] == ["u2", "u3"]
    session.reset()
    assert session.turns == []


def test_build_task_payload_has_local_history_and_no_auto_execute():
    session = cli.ChatSession()
    session.add("prima", "risposta")
    payload = cli.build_task_payload("seconda", session)
    terminal = payload["extra_context"]["terminal_client"]
    assert terminal["auto_execute_protected_actions"] is False
    assert terminal["conversation_history"] == [{"user": "prima", "ralf": "risposta"}]


def test_interactive_commands_raw_reset_exit():
    inputs = iter(["/raw", "ciao", "/history", "/reset", "/exit"])
    out = io.StringIO()
    rc = cli.run_chat(
        argparse.Namespace(base_url=None, timeout=None, raw=False, no_history=False),
        client=FakeClient([{"final_answer": "ok"}]),
        input_func=lambda prompt: next(inputs),
        out=out,
        err=io.StringIO(),
    )
    text = out.getvalue()
    assert rc == 0
    assert "raw=on" in text
    assert '"final_answer": "ok"' in text
    assert "sessione locale azzerata" in text


def test_interactive_exit_and_eof():
    out = io.StringIO()
    rc = cli.run_chat(
        argparse.Namespace(base_url=None, timeout=None, raw=False, no_history=False),
        client=FakeClient(),
        input_func=lambda prompt: (_ for _ in ()).throw(EOFError()),
        out=out,
        err=io.StringIO(),
    )
    assert rc == 0


def test_status_and_endpoint_do_not_need_privileges():
    out = io.StringIO()
    inputs = iter(["/status", "/endpoint", "/exit"])
    rc = cli.run_chat(
        argparse.Namespace(base_url=None, timeout=None, raw=False, no_history=False),
        client=FakeClient(),
        input_func=lambda prompt: next(inputs),
        out=out,
        err=io.StringIO(),
    )
    text = out.getvalue()
    assert rc == 0
    assert "/tasks/run" in text
    assert "hmac_key_file" in text
    assert "[REDACTED]" in text


def test_env_config(monkeypatch):
    monkeypatch.setenv("RALF_BASE_URL", "http://example.test")
    monkeypatch.setenv("RALF_CHAT_TIMEOUT", "7")
    monkeypatch.setenv("RALF_CHAT_HISTORY_LIMIT", "3")
    cfg = cli.ChatConfig.from_env()
    assert cfg.base_url == "http://example.test"
    assert cfg.timeout == 7
    assert cfg.history_limit == 3


def test_help_parser_contains_commands():
    parser = cli.build_parser()
    assert parser.parse_args(["ask", "ciao"]).command == "ask"
    assert parser.parse_args(["chat"]).command == "chat"


def test_invalid_json_error(monkeypatch):
    class BadClient(cli.RalfHTTPClient):
        def post_task(self, payload):
            raise cli.RalfTerminalError("invalid_json")

    err = io.StringIO()
    args = argparse.Namespace(message=["x"], base_url=None, timeout=None, raw=False, json=False, no_history=False)
    assert cli.run_ask(args, client=BadClient(), out=io.StringIO(), err=err) == 1
    assert "invalid_json" in err.getvalue()
