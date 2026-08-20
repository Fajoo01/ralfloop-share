from __future__ import annotations

import io
import json

import pytest

from ralfloop_agent.cli import terminal_chat


def _args(*argv: str):
    return terminal_chat.build_parser().parse_args(list(argv))


def test_portal_read_only_actions_use_expected_endpoints(monkeypatch) -> None:
    calls = []

    def fake_request(method, path, *, payload=None, timeout=30.0):
        calls.append((method, path, payload, timeout))
        return {"ok": True, "path": path}

    monkeypatch.setattr(terminal_chat, "_repair_backend_request", fake_request)

    for action in ("arci-profile", "support4youth-snapshot"):
        out = io.StringIO()
        assert terminal_chat.run_portal_command(
            _args("portal", action),
            out=out,
            err=io.StringIO(),
        ) == 0
        assert json.loads(out.getvalue())["ok"] is True

    assert calls == [
        ("GET", "/portals/arci/profile", None, 30.0),
        ("GET", "/portals/support4youth/snapshot", None, 30.0),
    ]


def test_portal_preview_and_request_validate_and_forward_operations(monkeypatch) -> None:
    calls = []
    operations = [{"kind": "fill", "target": "organization.name", "value": "Tiremm"}]
    encoded = json.dumps(operations)

    def fake_request(method, path, *, payload=None, timeout=30.0):
        calls.append((method, path, payload))
        return {"status": "ok"}

    monkeypatch.setattr(terminal_chat, "_repair_backend_request", fake_request)

    for action in ("support4youth-preview", "support4youth-request"):
        assert terminal_chat.run_portal_command(
            _args("portal", action, "--operations-json", encoded),
            out=io.StringIO(),
            err=io.StringIO(),
        ) == 0

    assert calls == [
        (
            "POST",
            "/portals/support4youth/preview",
            {"operations": operations},
        ),
        (
            "POST",
            "/portals/support4youth/requests",
            {"operations": operations, "requested_by": "ralf_portal_cli"},
        ),
    ]


def test_portal_send_updates_is_explicit_separate_phase(monkeypatch) -> None:
    calls = []

    def fake_request(method, path, *, payload=None, timeout=30.0):
        calls.append((method, path, payload, timeout))
        return {"status": "preview" if method == "GET" else "pending"}

    monkeypatch.setattr(terminal_chat, "_repair_backend_request", fake_request)

    for action in (
        "support4youth-send-updates-preview",
        "support4youth-send-updates-request",
    ):
        assert terminal_chat.run_portal_command(
            _args("portal", action),
            out=io.StringIO(),
            err=io.StringIO(),
        ) == 0

    assert calls == [
        (
            "GET",
            "/portals/support4youth/send-updates/preview",
            None,
            30.0,
        ),
        (
            "POST",
            "/portals/support4youth/send-updates/requests",
            {"requested_by": "ralf_portal_cli"},
            30.0,
        ),
    ]


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ("not-json", "portal_operations_json_invalid"),
        ("{}", "portal_operations_must_be_array"),
        ("[]", "portal_operations_count_invalid"),
        (json.dumps([{}] * 65), "portal_operations_count_invalid"),
        ('["not-object"]', "portal_operation_must_be_object"),
    ],
)
def test_portal_operations_fail_closed_before_backend(monkeypatch, raw, error) -> None:
    monkeypatch.setattr(
        terminal_chat,
        "_repair_backend_request",
        lambda *args, **kwargs: pytest.fail("backend must not be called"),
    )
    err = io.StringIO()

    assert terminal_chat.run_portal_command(
        _args("portal", "support4youth-preview", "--operations-json", raw),
        out=io.StringIO(),
        err=err,
    ) == 2
    assert error in err.getvalue()


def test_portal_apply_validates_request_id_and_uses_expected_endpoint(monkeypatch) -> None:
    calls = []

    def fake_request(method, path, *, payload=None, timeout=30.0):
        calls.append((method, path, payload, timeout))
        return {"status": "executed"}

    monkeypatch.setattr(terminal_chat, "_repair_backend_request", fake_request)
    out = io.StringIO()

    assert terminal_chat.run_portal_command(
        _args("portal", "support4youth-apply", "apr_ABCD2345"),
        out=out,
        err=io.StringIO(),
    ) == 0
    assert calls == [
        (
            "POST",
            "/portals/support4youth/requests/apr_ABCD2345/apply",
            None,
            60.0,
        )
    ]

    err = io.StringIO()
    assert terminal_chat.run_portal_command(
        _args("portal", "support4youth-apply", "../escape"),
        out=io.StringIO(),
        err=err,
    ) == 2
    assert "portal_request_id_invalid" in err.getvalue()
    assert len(calls) == 1


@pytest.mark.parametrize(
    "request_id",
    ["apr_ABC123", "apr_abcd2345", "req_ABCD2345", "apr_ABCD23450"],
)
def test_portal_apply_requires_exact_approval_id(monkeypatch, request_id) -> None:
    monkeypatch.setattr(
        terminal_chat,
        "_repair_backend_request",
        lambda *args, **kwargs: pytest.fail("backend must not be called"),
    )

    assert terminal_chat.run_portal_command(
        _args("portal", "support4youth-apply", request_id),
        out=io.StringIO(),
        err=io.StringIO(),
    ) == 2


@pytest.mark.parametrize(
    "result",
    [
        {"ok": False},
        {"status": "AUTH_REQUIRED"},
        {"status": "operations_invalid"},
        {"status": "execution_failed"},
    ],
)
def test_portal_backend_failure_result_is_nonzero(monkeypatch, result) -> None:
    monkeypatch.setattr(
        terminal_chat,
        "_repair_backend_request",
        lambda *args, **kwargs: result,
    )
    out = io.StringIO()

    assert terminal_chat.run_portal_command(
        _args("portal", "arci-profile"),
        out=out,
        err=io.StringIO(),
    ) == 1
    assert json.loads(out.getvalue()) == result
