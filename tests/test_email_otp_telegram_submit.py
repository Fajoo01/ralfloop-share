import json
import time

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains import telegram_approval_api as api


class FakeStore:
    def __init__(self):
        self.row = {
            "request_id": "apr_TESTOTP1",
            "action": "send_email",
            "status": "approved",
            "expires_at": int(time.time()) + 600,
            "consumed_at": None,
            "scope": {"recipient": "fabio@example.invalid"},
        }

    def get_request(self, request_id):
        return self.row if request_id == self.row["request_id"] else None


class FakeGate:
    def __init__(self, *, matches=True, verified=True):
        self.matches = matches
        self.verified = verified
        self.submissions = []

    def binding_for_otp_request(self, otp_request_id):
        if otp_request_id != "mailotp_test123":
            return None
        return {
            "approval_request_id": "apr_TESTOTP1",
            "otp_request_id": otp_request_id,
            "status": "pending",
            "otp_scope": {"recipient": "fabio@example.invalid"},
        }

    def binding_matches_approval(self, binding, approval):
        return self.matches

    def submit_telegram_reply(self, approval, *, otp, telegram_chat_id, telegram_message_id):
        self.submissions.append((approval["request_id"], otp, telegram_chat_id, telegram_message_id))
        return {"approved": self.verified}


class FakeExecutor:
    def __init__(self):
        self.calls = []

    def execute_approved_request(self, request_id):
        self.calls.append(request_id)
        return {"status": "executed", "sent": True, "retry_allowed": False}

def _payload(**updates):
    value = {
        "otp_request_id": "mailotp_test123",
        "otp": "123456",
        "telegram_user_id": 11,
        "telegram_chat_id": 11,
        "telegram_message_id": 77,
        "chat_type": "private",
    }
    value.update(updates)
    return value


def _policy():
    return DomainApprovalPolicy(
        enabled=True,
        allowed_user_ids={11},
        allowed_chat_ids={11},
        require_private_chat=True,
    )


def test_exact_replied_otp_executes_immutable_approval(monkeypatch):
    monkeypatch.setattr(api, "verify_hmac_headers", lambda **kwargs: {"ok": True, "status": "ok"})
    gate, executor, store = FakeGate(), FakeExecutor(), FakeStore()
    body = json.dumps(_payload()).encode()
    result = api.handle_email_otp_submit_request(
        body=body, headers={}, policy=_policy(), store=store,
        email_otp_gate=gate, email_executor=executor,
    )

    assert result["status"] == "executed"
    assert result["sent"] is True
    assert executor.calls == ["apr_TESTOTP1"]
    assert gate.submissions == [("apr_TESTOTP1", "123456", 11, 77)]


def test_email_otp_wrong_telegram_identity_fails_closed(monkeypatch):
    monkeypatch.setattr(api, "verify_hmac_headers", lambda **kwargs: {"ok": True, "status": "ok"})
    executor = FakeExecutor()
    result = api.handle_email_otp_submit_request(
        body=json.dumps(_payload(telegram_user_id=99)).encode(), headers={},
        policy=_policy(), store=FakeStore(), email_otp_gate=FakeGate(),
        email_executor=executor,
    )

    assert result == {"status": "email_otp_source_denied", "sent": False}
    assert executor.calls == []


def test_email_otp_scope_mismatch_fails_closed(monkeypatch):
    monkeypatch.setattr(api, "verify_hmac_headers", lambda **kwargs: {"ok": True, "status": "ok"})
    executor = FakeExecutor()
    result = api.handle_email_otp_submit_request(
        body=json.dumps(_payload()).encode(), headers={},
        policy=_policy(), store=FakeStore(), email_otp_gate=FakeGate(matches=False),
        email_executor=executor,
    )

    assert result == {"status": "email_otp_scope_mismatch", "sent": False}
    assert executor.calls == []


def test_email_otp_invalid_code_never_reaches_executor(monkeypatch):
    monkeypatch.setattr(api, "verify_hmac_headers", lambda **kwargs: {"ok": True, "status": "ok"})
    executor = FakeExecutor()
    result = api.handle_email_otp_submit_request(
        body=json.dumps(_payload(otp="12345")).encode(), headers={},
        policy=_policy(), store=FakeStore(), email_otp_gate=FakeGate(),
        email_executor=executor,
    )

    assert result == {"status": "email_otp_request_invalid", "sent": False}
    assert executor.calls == []
