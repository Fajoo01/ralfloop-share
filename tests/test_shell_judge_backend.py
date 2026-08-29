from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException

import openshell_backend.app as backend
from ralfloop_agent.shell_judge import ShellDecision


@pytest.fixture(scope="session")
def backend_parser(tmp_path_factory):
    root = Path(__file__).resolve().parents[1]
    output = tmp_path_factory.mktemp("backend-shell-judge") / "ralf-shell-ast"
    subprocess.run(["go", "build", "-trimpath", "-o", str(output), "./judge.go"],
                   cwd=root / "ralfloop_agent/shell_judge/mvdan", check=True)
    return output


@pytest.fixture(autouse=True)
def configure_backend_parser(monkeypatch, backend_parser):
    monkeypatch.setenv("RALF_SHELL_JUDGE_PARSER", str(backend_parser))


def _sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(backend, "BASE_DIR", tmp_path)
    root = tmp_path / "safe" / "workspace"
    root.mkdir(parents=True)
    return root


def test_backend_denies_before_bash_execution(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(backend, "audit", lambda *args, **kwargs: None)
    original = subprocess.run
    bash_calls = []

    def spy(argv, *args, **kwargs):
        if argv[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]:
            bash_calls.append(argv)
            raise AssertionError("bash_must_not_run")
        return original(argv, *args, **kwargs)

    monkeypatch.setattr(backend.subprocess, "run", spy)
    with pytest.raises(HTTPException) as caught:
        backend.exec_in_sandbox("safe", backend.ExecRequest(command="rm -rf /home"))
    assert caught.value.status_code == 403
    assert caught.value.detail["decision"] == ShellDecision.DENY.value
    assert bash_calls == []


def test_backend_review_blocks_execution(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    monkeypatch.setattr(backend, "audit", lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as caught:
        backend.exec_in_sandbox("safe", backend.ExecRequest(command="unknown-command"))
    assert caught.value.detail == {
        "decision": "REVIEW", "reason": "unknown_command", "reviewer_required": True,
    }


def test_compatibility_wrapper_delegates(tmp_path):
    allowed, reason = backend.check_command_allowed("git status", cwd=tmp_path)
    assert allowed is True
    assert reason == "allowlisted_read_only"


def test_audit_uses_digest_not_raw_command(tmp_path, monkeypatch):
    _sandbox(tmp_path, monkeypatch)
    rows = []
    monkeypatch.setattr(backend, "audit", lambda event, **fields: rows.append((event, fields)))
    with pytest.raises(HTTPException):
        backend.exec_in_sandbox("safe", backend.ExecRequest(command="unknown-command secret-value"))
    assert rows
    assert "command_digest" in rows[-1][1]
    assert "deterministic_reason" in rows[-1][1]
    assert "path_classes" in rows[-1][1]
    assert "event_id" in rows[-1][1]
    assert "secret-value" not in repr(rows[-1])


@pytest.mark.parametrize("failure,event", [
    (subprocess.TimeoutExpired(["/bin/bash"], 1), "exec_timeout"),
    (OSError("RALF_SECRET_MARKER_93A7"), "exec_exception"),
])
def test_audit_failure_paths_never_log_raw_command(tmp_path, monkeypatch, failure, event):
    root = _sandbox(tmp_path, monkeypatch)
    (root / "RALF_SECRET_MARKER_93A7").write_text("safe", encoding="utf-8")
    rows = []
    monkeypatch.setattr(backend, "audit", lambda name, **fields: rows.append((name, fields)))
    original = subprocess.run
    def fail_bash(argv, *args, **kwargs):
        if argv[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]:
            raise failure
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(backend.subprocess, "run", fail_bash)
    request = backend.ExecRequest(command="cat RALF_SECRET_MARKER_93A7", timeout_sec=1)
    if isinstance(failure, subprocess.TimeoutExpired):
        backend.exec_in_sandbox("safe", request)
    else:
        with pytest.raises(OSError):
            backend.exec_in_sandbox("safe", request)
    assert rows[-1][0] == event
    assert "RALF_SECRET_MARKER_93A7" not in repr(rows[-1])
    assert rows[-1][1]["execution_allowed"] is True


def test_success_audit_has_complete_redacted_envelope(tmp_path, monkeypatch):
    root = _sandbox(tmp_path, monkeypatch)
    marker = "RALF_SECRET_MARKER_93A7"
    (root / marker).write_text("safe", encoding="utf-8")
    rows = []
    monkeypatch.setattr(backend, "audit", lambda event, **fields: rows.append((event, fields)))
    original = subprocess.run
    def fake_bash(argv, *args, **kwargs):
        if argv[:4] == ["/bin/bash", "--noprofile", "--norc", "-c"]:
            return subprocess.CompletedProcess(argv, 0, "safe", "")
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(backend.subprocess, "run", fake_bash)
    result = backend.exec_in_sandbox("safe", backend.ExecRequest(command=f"cat {marker}"))
    assert result["ok"] is True
    event, fields = rows[-1]
    assert event == "exec"
    assert marker not in repr(rows)
    for key in ("event_id", "command_digest", "decision", "deterministic_reason",
                "destructive_level", "path_classes", "reviewer_used", "execution_allowed"):
        assert key in fields
