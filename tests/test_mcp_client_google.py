from __future__ import annotations

import pytest

from ralfloop_agent.integration.confirmation_store import (
    confirm_action,
    get_persistent_confirmation,
    pending_actions,
    reject_action,
    request_confirmation,
)
from src import audit
from src.confirmation import confirmation_manager
from src.google_client_fake import GoogleClientFake
from src.mcp_client import MCPClient, NeedsConfirmationError


@pytest.fixture(autouse=True)
def isolated_confirmation_store(monkeypatch, tmp_path):
    monkeypatch.setenv("RALF_CONFIRMATION_DB_PATH", str(tmp_path / "confirmations.sqlite"))
    _reset_confirmations()
    yield
    confirmation_manager.pending.clear()
    pending_actions.clear()


def _reset_confirmations() -> None:
    confirmation_manager.pending.clear()
    pending_actions.clear()


def test_send_email_with_google_enabled(monkeypatch):
    _reset_confirmations()
    monkeypatch.setenv("RALF_MCP_GOOGLE_ENABLED", "1")
    client = MCPClient(google_client=GoogleClientFake(draft_only=True))

    with pytest.raises(NeedsConfirmationError) as exc:
        client.send_email("user@example.invalid", "Subject", "Body")

    action = pending_actions[exc.value.confirmation_id]
    assert action["status"] == "pending"
    assert action["details"]["draft_only"] is True


def test_send_email_with_google_disabled(monkeypatch):
    monkeypatch.setenv("RALF_MCP_GOOGLE_ENABLED", "0")
    client = MCPClient()

    with pytest.raises(NotImplementedError):
        client.send_email("user@example.invalid", "Subject", "Body")


def test_confirm_send_email(monkeypatch):
    _reset_confirmations()
    monkeypatch.setenv("RALF_MCP_GOOGLE_ENABLED", "1")
    fake = GoogleClientFake(draft_only=True)
    client = MCPClient(google_client=fake)

    with pytest.raises(NeedsConfirmationError) as exc:
        client.send_email("user@example.invalid", "Subject", "Body")

    assert confirm_action(exc.value.confirmation_id) is True
    action = pending_actions[exc.value.confirmation_id]
    assert action["executed"] is True
    assert action["status"] == "executed"
    assert action["result"]["status"] == "draft_created"
    assert fake.drafts[0]["to"] == "user@example.invalid"


def test_confirm_send_email_when_draft_only_false(monkeypatch):
    _reset_confirmations()
    monkeypatch.setenv("RALF_MCP_GOOGLE_ENABLED", "1")
    fake = GoogleClientFake(draft_only=False)
    client = MCPClient(google_client=fake)

    with pytest.raises(NeedsConfirmationError) as exc:
        client.send_email("user@example.invalid", "Subject", "Body")

    assert confirm_action(exc.value.confirmation_id) is True
    action = pending_actions[exc.value.confirmation_id]
    assert action["status"] == "executed"
    assert action["result"]["status"] == "sent"
    assert action["result"]["draft_only"] is False
    assert fake.sent[0]["to"] == "user@example.invalid"


def test_confirm_send_email_writes_persistent_audit(monkeypatch, tmp_path):
    _reset_confirmations()
    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(audit, "AUDIT_PATH", audit_path)
    monkeypatch.setenv("RALF_MCP_GOOGLE_ENABLED", "1")
    client = MCPClient(google_client=GoogleClientFake(draft_only=False))

    with pytest.raises(NeedsConfirmationError) as exc:
        client.send_email("user@example.invalid", "Subject", "Body")

    assert confirm_action(exc.value.confirmation_id) is True
    text = audit_path.read_text(encoding="utf-8")
    assert "mcp_send_email_executed" in text
    assert "confirmation_executed" in text
    assert "user@example.invalid" in text


def test_confirmation_is_persisted(monkeypatch):
    monkeypatch.setenv("RALF_MCP_GOOGLE_ENABLED", "1")
    client = MCPClient(google_client=GoogleClientFake(draft_only=True))

    with pytest.raises(NeedsConfirmationError) as exc:
        client.send_email("user@example.invalid", "Subject", "Body")

    persisted = get_persistent_confirmation(exc.value.confirmation_id)
    assert persisted is not None
    assert persisted["status"] == "pending"
    assert persisted["action_type"] == "send_email"
    assert persisted["details"]["to"] == "user@example.invalid"


def test_approve_executes_only_once():
    calls = {"count": 0}

    def action() -> dict:
        calls["count"] += 1
        return {"status": "done", "count": calls["count"]}

    confirmation_id = request_confirmation("unit_action", {"body_preview": "ok"}, action_fn=action)

    assert confirm_action(confirmation_id) is True
    assert confirm_action(confirmation_id) is True
    assert calls["count"] == 1
    assert pending_actions[confirmation_id]["status"] == "executed"
    assert get_persistent_confirmation(confirmation_id)["status"] == "executed"


def test_reject_does_not_execute():
    calls = {"count": 0}

    def action() -> dict:
        calls["count"] += 1
        return {"status": "done"}

    confirmation_id = request_confirmation("unit_action", {"body_preview": "ok"}, action_fn=action)

    assert reject_action(confirmation_id) is True
    assert confirm_action(confirmation_id) is False
    assert calls["count"] == 0
    assert get_persistent_confirmation(confirmation_id)["status"] == "rejected"


def test_failed_confirmation_does_not_retry():
    calls = {"count": 0}

    def action() -> dict:
        calls["count"] += 1
        raise RuntimeError("boom")

    confirmation_id = request_confirmation("unit_action", {"body_preview": "ok"}, action_fn=action)

    assert confirm_action(confirmation_id) is False
    assert confirm_action(confirmation_id) is False
    assert calls["count"] == 1
    persisted = get_persistent_confirmation(confirmation_id)
    assert persisted["status"] == "failed"
    assert persisted["error"] == "boom"
