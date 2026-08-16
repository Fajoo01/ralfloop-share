from __future__ import annotations

from contextlib import contextmanager
import sys

import pytest

import tools.run_magnolia_workflow as launcher


def test_shadow_launcher_uses_no_persistent_store_and_no_forced_normal(monkeypatch, tmp_path, capsys):
    policy = type("Policy", (), {"enabled": False, "auto_execute": True})()
    monkeypatch.setenv("RALFLOOP_SEMANTIC_JUDGE_SHADOW", "1")
    monkeypatch.setenv("RALFLOOP_SEMANTIC_JUDGE_SHADOW_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setattr(launcher.DomainApprovalPolicy, "from_env", classmethod(lambda cls: policy))
    monkeypatch.setattr(
        launcher,
        "DomainApprovalStore",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("persistent_store_created")),
    )

    @contextmanager
    def session():
        yield object()

    captured = {}

    class Workflow:
        def __init__(self, gateway, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(launcher, "build_session_from_env", session)
    monkeypatch.setattr(launcher, "GoogleWorkspaceGateway", lambda session, account: object())
    monkeypatch.setattr(launcher, "MagnoliaWorkflow", Workflow)
    monkeypatch.setattr(launcher, "RalfReplyGenerator", lambda: object())
    monkeypatch.setattr(launcher, "LocalRouter", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher.ToolRegistry, "load", classmethod(lambda cls, path: object()))
    monkeypatch.setattr(launcher, "FunctionGemmaClient", lambda: object())
    monkeypatch.setattr(
        launcher,
        "dispatch_magnolia_request",
        lambda *args, **kwargs: {
            "status": "shadow_complete",
            "artifact_path": str(tmp_path / "case.json"),
            "risk": {"level": "high", "reasons": ["uncertain_fact"]},
            "ds4_invoked": True,
            "approval": None,
            "email_sent": False,
        },
    )
    monkeypatch.setattr(sys, "argv", ["run_magnolia_workflow.py"])
    assert launcher.main() == 0
    assert isinstance(captured["approval_store"], launcher.ShadowApprovalStore)
    assert "risk_classification" not in captured
    assert not (tmp_path / "forbidden-outbox.jsonl").exists()
    assert '"status": "shadow_complete"' in capsys.readouterr().out


def test_shadow_store_fails_closed_if_workflow_regresses_into_approval():
    with pytest.raises(RuntimeError, match="shadow_approval_persistence_forbidden"):
        launcher.ShadowApprovalStore().create_request(action="reply_email")
