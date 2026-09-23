from __future__ import annotations

import os
from pathlib import Path

import pytest

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.digital_signing import (
    ArubaSignApprovalWorkflow,
    DigitalSigningError,
)
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


def _store(tmp_path: Path) -> DomainApprovalStore:
    policy = DomainApprovalPolicy(
        enabled=True,
        ttl_sec=3600,
        max_pending=20,
        require_private_chat=True,
        db_path=str(tmp_path / "approval.sqlite3"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    return DomainApprovalStore(policy=policy)


def _fake_arubasign(tmp_path: Path) -> Path:
    path = tmp_path / "ArubaSign"
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _workflow(tmp_path: Path) -> ArubaSignApprovalWorkflow:
    openssl = Path("/usr/bin/openssl")
    if not openssl.is_file():
        pytest.skip("openssl unavailable")
    return ArubaSignApprovalWorkflow(
        _store(tmp_path),
        arubasign_path=_fake_arubasign(tmp_path),
        openssl_path=openssl,
        allowed_roots=(tmp_path,),
    )


def _approve(store: DomainApprovalStore, request: dict) -> dict:
    decision = DomainApprovalDecision(
        request_id=request["request_id"],
        decision="approve",
        telegram_user_id=1,
        telegram_chat_id=1,
        telegram_message_id=1,
        chat_type="private",
        idempotency_key="test-approve-" + request["request_id"],
    )
    return store.decide(decision, scope_digest_short=request["scope_digest_short"])


def test_prepare_binds_exact_hash_and_never_stores_credentials(tmp_path, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("BOTTAZZI_ARUBASIGN_DISPLAY", raising=False)
    workflow = _workflow(tmp_path)
    source = tmp_path / "modulo-compilato.docx"
    source.write_bytes(b"completed form")

    result = workflow.prepare(source, requested_by="test")

    assert result["status"] == "approval_required"
    assert result["gui_available"] is False
    assert result["secrets_managed_by_bottazzi"] is False
    request = workflow.store.get_request(result["approval_request_id"])
    assert request is not None
    assert request["scope"]["source_sha256"] == result["source_sha256"]
    assert request["scope"]["send_authorized"] is False
    assert request["scope"]["pin_otp_policy"] == "user_entry_only_never_stored"
    scope_keys = {str(key).casefold() for key in request["scope"]}
    assert scope_keys.isdisjoint({"password", "otp", "pin", "credential", "secret", "token"})
    assert request["scope"]["pin_otp_policy"] == "user_entry_only_never_stored"


def test_handoff_requires_approval_and_gui_but_does_not_consume(tmp_path, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("BOTTAZZI_ARUBASIGN_DISPLAY", raising=False)
    workflow = _workflow(tmp_path)
    source = tmp_path / "modulo.docx"
    source.write_bytes(b"completed form")
    prepared = workflow.prepare(source)

    blocked = workflow.handoff_approved(prepared["approval_request_id"])
    assert blocked["status"].startswith("not_approved:")
    assert blocked["launched"] is False

    request = workflow.store.get_request(prepared["approval_request_id"])
    assert request is not None
    assert _approve(workflow.store, request)["status"] == "approved"
    handoff = workflow.handoff_approved(prepared["approval_request_id"])
    assert handoff["status"] == "user_interaction_required"
    assert handoff["launched"] is False
    assert handoff["secrets_managed_by_bottazzi"] is False
    assert workflow.store.get_request(prepared["approval_request_id"])["status"] == "approved"


def test_source_change_invalidates_approved_signature_scope(tmp_path, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("BOTTAZZI_ARUBASIGN_DISPLAY", raising=False)
    workflow = _workflow(tmp_path)
    source = tmp_path / "modulo.docx"
    source.write_bytes(b"version one")
    prepared = workflow.prepare(source)
    request = workflow.store.get_request(prepared["approval_request_id"])
    assert request is not None
    assert _approve(workflow.store, request)["status"] == "approved"

    source.write_bytes(b"version two")
    result = workflow.handoff_approved(prepared["approval_request_id"])
    assert result["status"] == "stale"
    assert workflow.store.get_request(prepared["approval_request_id"])["status"] == "stale"


def test_symlink_and_outside_root_are_denied(tmp_path):
    workflow = _workflow(tmp_path)
    outside = tmp_path.parent / "outside-signing-test.txt"
    outside.write_bytes(b"outside")
    try:
        with pytest.raises(DigitalSigningError, match="outside_allowed_roots"):
            workflow.prepare(outside)
        link = tmp_path / "link.docx"
        link.symlink_to(outside)
        with pytest.raises(DigitalSigningError, match="symlink_denied"):
            workflow.prepare(link)
    finally:
        outside.unlink(missing_ok=True)


def test_unified_runtime_surfaces_hash_bound_sign_approval(tmp_path, monkeypatch):
    from ralfloop_agent.unified_assistant.runtime import run_unified_telegram

    source = tmp_path / "modulo.docx"
    source.write_bytes(b"completed test form")
    fake_aruba = _fake_arubasign(tmp_path)
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "runtime-approval.sqlite3"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "runtime-audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("BOTTAZZI_SIGN_ALLOWED_ROOTS", str(tmp_path))
    monkeypatch.setenv("BOTTAZZI_ARUBASIGN_PATH", str(fake_aruba))
    monkeypatch.setenv("BOTTAZZI_OPENSSL_PATH", "/usr/bin/openssl")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("BOTTAZZI_ARUBASIGN_DISPLAY", raising=False)

    result = run_unified_telegram(
        f"Firma digitalmente {source}",
        {
            "source": "ralf_terminal",
            "terminal_client": {"session_id": "sign-runtime-test"},
            "requested_by": "test",
            "assistant_surface": "assistant_v1",
        },
    )

    assert result["ok"] is True
    assert result["capability"] == "documents.sign"
    assert result["approval_required"] is True
    assert str(result["approval_request_id"]).startswith("apr_")
    assert len(str(result["scope_digest_short"])) == 9
    assert len(str(result["source_sha256"])) == 64
    assert result["pin_otp_user_only"] is True
    assert result["send_authorized"] is False
    assert result["writes"] == 0
    assert result["sends"] == 0


def test_planner_routes_prepare_handoff_and_verify_signing():
    planner = UnifiedPlanner(UnifiedRegistryFacade())

    prepare = planner.validate(planner.plan(
        "Firma digitalmente /var/lib/ralfloop/pec-outbox/modulo.docx"
    ))
    assert prepare.intent == "documents.sign"
    assert prepare.assignments[0].policy.value == "CONFIRM_WRITE"
    assert prepare.assignments[0].arguments["operation"] == "prepare"
    assert prepare.assignments[0].arguments["source_path"].endswith("modulo.docx")

    handoff = planner.validate(planner.plan("Procedi con la firma apr_ABCDEFGH in ArubaSign"))
    assert handoff.assignments[0].arguments == {
        "operation": "handoff",
        "approval_request_id": "apr_ABCDEFGH",
    }

    verify = planner.validate(planner.plan(
        "Verifica la firma apr_ABCDEFGH /var/lib/ralfloop/pec-outbox/modulo.docx.p7m"
    ))
    assert verify.assignments[0].arguments["operation"] == "verify"
    assert verify.assignments[0].arguments["approval_request_id"] == "apr_ABCDEFGH"
    assert verify.assignments[0].arguments["signed_path"].endswith(".p7m")
