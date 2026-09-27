from __future__ import annotations

import json

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant import runtime


def _context(*, user=123, chat=123, message=1):
    return {
        "source": "telegram_natural",
        "telegram_user_id": user,
        "telegram_chat_id": chat,
        "telegram_message_id": message,
    }


def _configure(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approvals.sqlite3"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "999")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "999")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_REQUIRE_PRIVATE_CHAT", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX", str(tmp_path / "outbox.jsonl"))
    monkeypatch.setenv(
        "TIREMM_CIAV_ACCOUNT_RECOVERY_URL",
        "https://remote.tiremminnanz.com/account/recover/",
    )


def test_ciav_forgotten_password_is_routed_and_keeps_vault_zero_knowledge(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    text = "Maria ha perso la password del CIAV"
    context = _context()

    assert runtime.is_unified_telegram_request(text, context) is True
    result = runtime.run_unified_telegram(text, context)

    assert result["status"] == "recovery_guidance"
    assert result["capability"] == "ciav_password_recovery"
    assert result["metadata"]["vault_master_password_recoverable"] is False
    assert result["metadata"]["data_deleted"] is False
    assert result["metadata"]["writes"] == 0
    assert "https://remote.tiremminnanz.com/account/recover/" in result["final_answer"]
    assert "zero-knowledge" in result["final_answer"]
    assert not (tmp_path / "outbox.jsonl").exists()


def test_ciav_recovery_is_private_telegram_only(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    result = runtime.run_unified_telegram(
        "ho dimenticato la password CIAV",
        _context(user=123, chat=-100123),
    )

    assert result["status"] == "denied"
    assert result["metadata"]["data_deleted"] is False
    assert result["metadata"]["writes"] == 0
    assert not (tmp_path / "outbox.jsonl").exists()


def test_ciav_reprovision_requires_explicit_data_loss_words():
    assert runtime._ciav_vault_reprovision_intent("ricrea CIAV") is False
    assert runtime._ciav_vault_reprovision_intent("azzera il Portachiavi") is False
    assert runtime._ciav_vault_reprovision_intent("ricrea CIAV e perdo i dati") is True


def test_ciav_explicit_reprovision_only_stages_admin_approval(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    context = _context(message=7)
    text = "ricrea CIAV e perdo i dati"

    first = runtime.run_unified_telegram(text, context)
    second = runtime.run_unified_telegram(text, context)

    assert first["status"] == "approval_requested"
    assert first["metadata"]["data_deleted"] is False
    assert first["metadata"]["writes"] == 0
    assert second["status"] == "approval_requested"
    assert second["metadata"]["duplicate"] is True
    assert second["metadata"]["approval_request_id"] == first["metadata"]["approval_request_id"]

    policy = DomainApprovalPolicy.from_env()
    pending = DomainApprovalStore(policy=policy).list_pending()
    assert len(pending) == 1
    request = pending[0]
    assert request["action"] == "ciav_vault_reprovision"
    assert request["scope"]["explicit_data_loss_consent"] is True
    assert request["scope"]["identity_binding"] == "requester_telegram_identity_only"
    assert request["scope"]["requester_telegram_user_id"] == 123
    assert "perdita dati" in request["telegram_message"]["message"].casefold()

    rows = [
        json.loads(line)
        for line in (tmp_path / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(rows) == 1
    assert rows[0]["request_id"] == first["metadata"]["approval_request_id"]


def test_ciav_reprovision_approval_is_not_auto_executable():
    from ralfloop_agent.domains import telegram_approval_api

    assert "ciav_vault_reprovision" not in telegram_approval_api.MCP_AUTO_EXECUTE_ACTIONS
