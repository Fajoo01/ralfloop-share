from __future__ import annotations

import json

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains import telegram_approval_api
from ralfloop_agent.unified_assistant import runtime


def configure(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approval.sqlite"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "999")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "999")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_REQUIRE_PRIVATE_CHAT", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX", str(tmp_path / "outbox.jsonl"))
    return tmp_path / "outbox.jsonl"


def telegram_context(**extra):
    return {
        "source": "telegram_natural",
        "telegram_user_id": extra.pop("user_id", 111),
        "telegram_chat_id": extra.pop("chat_id", 111),
        "telegram_message_id": extra.pop("message_id", 5),
        **extra,
    }


def test_recovery_request_goes_to_admin_not_requester(monkeypatch, tmp_path):
    outbox = configure(monkeypatch, tmp_path)
    result = runtime.run_unified_telegram(
        "ho dimenticato la password di BaffoFlix, account Maria",
        telegram_context(),
    )

    assert result["status"] == "approval_requested"
    assert result["approval_required"] is False
    assert "amministratore" in result["response"]
    assert "Non devi approvare nulla qui" in result["response"]
    assert result["metadata"]["writes"] == 0

    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)
    pending = store.list_pending()
    assert len(pending) == 1
    request = pending[0]
    assert request["action"] == "baffoflix_password_recovery"
    assert request["scope"]["username"] == "Maria"
    assert request["scope"]["requester_telegram_user_id"] == 111

    rows = [json.loads(line) for line in outbox.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["request_id"] == request["request_id"]


def test_recovery_requires_exact_username_and_trusted_telegram(monkeypatch, tmp_path):
    outbox = configure(monkeypatch, tmp_path)
    missing = runtime.run_unified_telegram(
        "ho dimenticato la password di BaffoFlix",
        telegram_context(),
    )
    assert missing["status"] == "clarification_required"
    assert missing["approval_required"] is False
    assert not outbox.exists()

    denied = runtime.run_unified_telegram(
        "ho dimenticato la password di BaffoFlix, account Maria",
        {
            "source": "ralf_terminal",
            "telegram_user_id": 111,
            "telegram_chat_id": 111,
            "telegram_message_id": 9,
        },
    )
    assert denied["status"] == "denied"
    assert denied["metadata"]["writes"] == 0
    assert not outbox.exists()


def test_username_followup_completes_recovery_request(monkeypatch, tmp_path):
    outbox = configure(monkeypatch, tmp_path)
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    first = runtime.run_unified_telegram(
        "ho dimenticato la password di BaffoFlix",
        telegram_context(message_id=5),
    )
    second = runtime.run_unified_telegram(
        "account Maria",
        telegram_context(message_id=6),
    )

    assert first["status"] == "clarification_required"
    assert second["status"] == "approval_requested"
    assert second["approval_required"] is False
    assert "amministratore" in second["response"]
    policy = DomainApprovalPolicy.from_env()
    pending = DomainApprovalStore(policy=policy).list_pending()
    assert len(pending) == 1
    assert pending[0]["scope"]["username"] == "Maria"
    assert len(outbox.read_text(encoding="utf-8").splitlines()) == 1


def test_duplicate_same_telegram_message_is_single_admin_request(monkeypatch, tmp_path):
    outbox = configure(monkeypatch, tmp_path)
    context = telegram_context()
    first = runtime.run_unified_telegram(
        "recupera la password di BaffoFlix, account Maria", context
    )
    second = runtime.run_unified_telegram(
        "recupera la password di BaffoFlix, account Maria", context
    )

    assert first["status"] == second["status"] == "approval_requested"
    assert first["metadata"]["approval_request_id"] == second["metadata"]["approval_request_id"]
    assert second["metadata"]["duplicate"] is True
    policy = DomainApprovalPolicy.from_env()
    assert len(DomainApprovalStore(policy=policy).list_pending()) == 1
    assert len(outbox.read_text(encoding="utf-8").splitlines()) == 1


def test_requester_cannot_approve_admin_request(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    result = runtime.run_unified_telegram(
        "reset password BaffoFlix account Maria", telegram_context()
    )
    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)
    request = store.get_request(result["metadata"]["approval_request_id"])

    decision = store.decide(
        DomainApprovalDecision(
            request_id=request["request_id"],
            decision="approve",
            telegram_user_id=111,
            telegram_chat_id=111,
            telegram_message_id=6,
            chat_type="private",
            idempotency_key="requester-cannot-approve",
        ),
        scope_digest_short=request["scope_digest_short"],
    )
    assert decision["status"] == "unauthorized"
    assert store.get_request(request["request_id"])["status"] == "pending"


def test_admin_approval_executes_recovery_once(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)
    created = store.create_request(
        action="baffoflix_password_recovery",
        bando_id="baffoflix.support",
        version="1",
        scope={
            "action": "baffoflix_password_recovery",
            "version": 1,
            "username": "Maria",
            "requester_telegram_user_id": 111,
            "requester_telegram_chat_id": 111,
            "requester_telegram_message_id": 5,
            "request_source": "telegram_natural",
        },
        requested_by="telegram:111",
    )["request"]
    approved = store.decide(
        DomainApprovalDecision(
            request_id=created["request_id"],
            decision="approve",
            telegram_user_id=999,
            telegram_chat_id=999,
            telegram_message_id=7,
            chat_type="private",
            idempotency_key="admin-approve",
        ),
        scope_digest_short=created["scope_digest_short"],
    )
    assert approved["status"] == "approved"

    calls = []

    def fake_recovery(username):
        calls.append(username)
        return {"ok": True, "status": "STARTED", "pin_file_exposed": False}

    monkeypatch.setattr(
        telegram_approval_api, "_call_baffoflix_password_recovery", fake_recovery
    )
    first = telegram_approval_api._execute_approved_mcp_request(
        created["request_id"], store=store
    )
    second = telegram_approval_api._execute_approved_mcp_request(
        created["request_id"], store=store
    )

    assert first["status"] == "executed"
    assert first["pin_file_exposed"] is False
    assert calls == ["Maria"]
    assert store.get_request(created["request_id"])["status"] == "consumed"
    assert second["status"] != "executed"
    assert calls == ["Maria"]


def test_signed_admin_telegram_approval_autoexecutes_recovery(monkeypatch, tmp_path):
    configure(monkeypatch, tmp_path)
    key = tmp_path / "approval.hmac"
    key.write_text("test-secret", encoding="utf-8")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE", str(key))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUTO_EXECUTE_MCP", "1")
    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)
    request = store.create_request(
        action="baffoflix_password_recovery",
        bando_id="baffoflix.support",
        version="1",
        scope={
            "action": "baffoflix_password_recovery",
            "version": 1,
            "username": "Maria",
            "requester_telegram_user_id": 111,
            "requester_telegram_chat_id": 111,
            "requester_telegram_message_id": 5,
            "request_source": "telegram_natural",
        },
        requested_by="telegram:111",
    )["request"]
    calls = []

    def fake_recovery(username):
        calls.append(username)
        return {"ok": True, "status": "STARTED", "pin_file_exposed": False}

    monkeypatch.setattr(
        telegram_approval_api, "_call_baffoflix_password_recovery", fake_recovery
    )
    payload = {
        "request_id": request["request_id"],
        "decision": "approve",
        "scope_digest_short": request["scope_digest_short"],
        "telegram_user_id": 999,
        "telegram_chat_id": 999,
        "telegram_message_id": 8,
        "chat_type": "private",
        "timestamp": 1,
        "idempotency_key": "signed-admin-approval",
    }
    path = f"/domain-approvals/{request['request_id']}/decision"
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    headers = telegram_approval_api.decision_headers(
        "POST", path, payload, key_file=key, nonce="baffoflix-test-nonce"
    )
    result = telegram_approval_api.handle_decision_request(
        request_id=request["request_id"], body=body, headers=headers,
        path=path, policy=policy, store=store,
    )

    assert result["status"] == "approved"
    assert result["auto_execute"] is True
    assert result["execution"]["status"] == "executed"
    assert calls == ["Maria"]
    assert store.get_request(request["request_id"])["status"] == "consumed"
