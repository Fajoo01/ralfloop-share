from __future__ import annotations

from pathlib import Path
import subprocess

import pytest
from fastapi import HTTPException

import openshell_backend.app as backend
from ralfloop_agent.shell_judge import ShellDecision


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
        if argv[:2] == ["/bin/bash", "-lc"]:
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
    assert "secret-value" not in repr(rows[-1])
