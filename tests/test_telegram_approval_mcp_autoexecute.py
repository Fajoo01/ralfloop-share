from __future__ import annotations

import json

from ralfloop_agent.domains import telegram_approval_api as api
from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy


class FakeStore:
    def __init__(self, action="mailchimp_campaign_send"):
        self.row = {"request_id": "apr_ABCDEFGH", "action": action, "scope": {"action": action}}
        self.decisions = 0
    def get_request(self, request_id):
        assert request_id == "apr_ABCDEFGH"
        return dict(self.row)
    def decide(self, decision, *, scope_digest_short):
        self.decisions += 1
        assert decision.decision == "approve"
        assert scope_digest_short == "ABCD-1234"
        return {"status": "approved", "request_id": decision.request_id, "auto_execute": False}
    def audit(self, *_args, **_kwargs):
        return None


def _body():
    return json.dumps({
        "decision": "approve", "scope_digest_short": "ABCD-1234",
        "telegram_user_id": 11, "telegram_chat_id": 22, "telegram_message_id": 33,
        "chat_type": "private", "timestamp": 1, "idempotency_key": "test",
    }).encode()


def test_approved_mcp_action_auto_executes_when_enabled(monkeypatch):
    store = FakeStore()
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUTO_EXECUTE_MCP", "1")
    monkeypatch.delenv("RALFLOOP_EMAIL_OTP_REQUIRED", raising=False)
    monkeypatch.setattr(api, "verify_hmac_headers", lambda **_kwargs: {"ok": True, "status": "ok"})
    monkeypatch.setattr(api, "_execute_approved_mcp_request", lambda request_id, store: {
        "status": "executed", "sent": True, "request_id": request_id,
    })
    result = api.handle_decision_request(
        request_id="apr_ABCDEFGH", body=_body(), headers={},
        policy=DomainApprovalPolicy(enabled=True), store=store,
    )
    assert result["status"] == "approved"
    assert result["auto_execute"] is True
    assert result["execution"]["status"] == "executed"


def test_approved_mcp_action_stays_approval_only_when_disabled(monkeypatch):
    store = FakeStore()
    monkeypatch.delenv("RALFLOOP_TELEGRAM_APPROVAL_AUTO_EXECUTE_MCP", raising=False)
    monkeypatch.delenv("RALFLOOP_EMAIL_OTP_REQUIRED", raising=False)
    monkeypatch.setattr(api, "verify_hmac_headers", lambda **_kwargs: {"ok": True, "status": "ok"})
    result = api.handle_decision_request(
        request_id="apr_ABCDEFGH", body=_body(), headers={},
        policy=DomainApprovalPolicy(enabled=True), store=store,
    )
    assert result == {"status": "approved", "request_id": "apr_ABCDEFGH", "auto_execute": False}
