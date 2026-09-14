from __future__ import annotations

from contextlib import AbstractContextManager

import pytest

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.conversation import ConversationManager
from ralfloop_agent.unified_assistant.email_send import (
    UnifiedEmailApprovalCoordinator,
    UnifiedGmailApprovalExecutor,
    build_email_approval_scope,
)
from src.google_workspace import GoogleWorkspaceGateway


ACCOUNT = "fabio@tiremminnanz.com"


class FakeSession:
    def __init__(self, *, fail_write=False, unverified=False, wrong_thread=False, ok_false=False,
                 real_markdown=False, source_to=ACCOUNT):
        self.fail_write = fail_write
        self.unverified = unverified
        self.wrong_thread = wrong_thread
        self.ok_false = ok_false
        self.real_markdown = real_markdown
        self.source_to = source_to
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if arguments["operation"] == "read":
            if self.real_markdown:
                return {"content": [{"type": "text", "text": """## Documenti

**From:** Marco <marco@example.invalid>
**To:** Fabio <fabio@tiremminnanz.com>
**Date:** Wed, 12 Aug 2026 10:00:00 +0200
**Labels:** INBOX

Documenti allegati."""}]}
            return {"structuredContent": {"message": {
                "messageId": arguments["messageId"],
                "threadId": "fedcba9876543210",
                "from": "Marco <marco@example.invalid>",
                "to": self.source_to,
                "subject": "Documenti",
                "date": "2026-08-11",
                "body": "Documenti allegati.",
            }}}
        if arguments["operation"] == "getThread":
            if self.real_markdown:
                return {"content": [{"type": "text", "text": """## Thread (1 messages)

**Marco <marco@example.invalid>** — Wed, 12 Aug 2026 10:00:00 +0200
Subject: Documenti
Documenti allegati."""}]}
            return {"structuredContent": {
                "threadId": arguments["threadId"],
                "messages": [{
                    "messageId": "1234567890abcdef",
                    "from": "Marco <marco@example.invalid>",
                    "to": ACCOUNT, "subject": "Documenti",
                    "date": "2026-08-11", "body": "Documenti allegati.",
                }],
            }}
        if self.fail_write:
            raise RuntimeError("provider_failed_after_call")
        if self.ok_false:
            return {"structuredContent": {"ok": False, "error": "provider_denied"}}
        if self.unverified:
            return {"structuredContent": {"ok": True}}
        if self.real_markdown:
            return {"content": [{"type": "text", "text":
                "Reply sent.\n\n**Message ID:** 0123456789abcdef"}]}
        return {"structuredContent": {"refs": {
            "id": "0123456789abcdef",
            "threadId": "0000000000000000" if self.wrong_thread else "fedcba9876543210",
        }}}


class Context(AbstractContextManager):
    def __init__(self, gateway):
        self.gateway = gateway

    def __enter__(self):
        return self.gateway

    def __exit__(self, *_):
        return None


def _approval(tmp_path, *, reply=False, cc="", bcc=""):
    policy = DomainApprovalPolicy(
        enabled=True,
        ttl_sec=300,
        max_pending=10,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        require_private_chat=True,
        db_path=str(tmp_path / "approvals.sqlite"),
    )
    store = DomainApprovalStore(policy=policy)
    manager = ConversationManager()
    payload = {
        "recipient": "marco@example.invalid",
        "subject": "Documenti",
        "body": "Grazie, documenti ricevuti.",
        "cc": cc,
        "bcc": bcc,
        "source_message_id": "1234567890abcdef" if reply else "",
        "thread_id": "fedcba9876543210" if reply else "",
        "approval_action": "reply_email" if reply else "send_email",
        "risk": "low",
        "validation_state": "passed",
    }
    pending = manager.stage(
        domain="email",
        action=payload["approval_action"],
        policy=PolicyClass.CONFIRM_WRITE,
        payload=payload,
        displayed_text="Bozza per Marco: Grazie, documenti ricevuti.",
    )
    coordinator = UnifiedEmailApprovalCoordinator(store, policy=policy, account=ACCOUNT)
    requested = coordinator.request(pending, requested_by="test")
    pending = manager.attach_approval_request(
        domain="email", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest,
        approval_ref=requested["request_id"],
        created_at=requested["created_at"], expires_at=requested["expires_at"],
    )
    approved = coordinator.approve(
        pending, telegram_user_id=11, telegram_chat_id=22,
        telegram_message_id=33, chat_type="private",
    )
    assert approved["status"] == "approved"
    pending = manager.bind_approval(
        domain="email", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest,
        approval_ref=str(pending.approval_ref),
    )
    return store, manager, pending


def _executor(store, session, *, account=ACCOUNT):
    gateway = GoogleWorkspaceGateway(session, account=account)
    gateway.discovered_tools = ("manage_email",)
    return UnifiedGmailApprovalExecutor(
        lambda: Context(gateway), store=store, account=account
    )


def test_scope_binds_account_recipient_subject_body_thread_draft_and_idempotency(tmp_path):
    _, _, pending = _approval(tmp_path, reply=True)

    scope = build_email_approval_scope(pending, account=ACCOUNT)

    assert scope["account"] == ACCOUNT
    assert scope["recipient"] == "marco@example.invalid"
    assert scope["subject"] == "Documenti"
    assert scope["body_sha256"]
    assert scope["thread_id"] == "fedcba9876543210"
    assert scope["draft_id"] == pending.pending_id
    assert scope["payload_digest"] == pending.payload_digest
    assert scope["idempotency_key"].startswith("email:")


def test_approved_send_forwards_hash_bound_bcc_to_gmail_provider(tmp_path):
    store, _, pending = _approval(tmp_path, bcc="info@tiremminnanz.com")
    session = FakeSession()
    gateway = GoogleWorkspaceGateway(session, account=ACCOUNT)
    gateway.discovered_tools = ("manage_email",)
    gateway._manage_email_fields = frozenset({
        "operation", "email", "to", "subject", "body", "cc", "bcc"
    })
    executor = UnifiedGmailApprovalExecutor(
        lambda: Context(gateway), store=store, account=ACCOUNT
    )

    result = executor.execute(pending)

    assert result["status"] == "executed"
    sends = [args for _, args in session.calls if args["operation"] == "send"]
    assert len(sends) == 1
    assert sends[0]["bcc"] == "info@tiremminnanz.com"
    assert "cc" not in sends[0]


def test_approval_request_queues_reply_bound_telegram_preview(tmp_path):
    policy = DomainApprovalPolicy(
        enabled=True, ttl_sec=300, max_pending=10,
        allowed_user_ids={11}, allowed_chat_ids={22},
        db_path=str(tmp_path / "approvals.sqlite"),
    )
    store = DomainApprovalStore(policy=policy)
    manager = ConversationManager()
    pending = manager.stage(
        domain="email", action="reply_email", policy=PolicyClass.CONFIRM_WRITE,
        payload={
            "recipient": "caterina@example.invalid", "subject": "Magnolia",
            "body": "Grazie, ci interessa partecipare.",
            "source_message_id": "1234567890abcdef",
            "thread_id": "fedcba9876543210",
        },
        displayed_text="Bozza Magnolia",
    )
    outbox = tmp_path / "outbox.jsonl"
    result = UnifiedEmailApprovalCoordinator(
        store, policy=policy, account=ACCOUNT, outbox_path=outbox,
    ).request(pending, requested_by="test")

    assert result["status"] == "pending"
    assert result["notification_queued"] is True
    row = __import__("json").loads(outbox.read_text(encoding="utf-8"))
    assert row["request_id"] == result["request_id"]
    assert f"Request: {result['request_id']}" in row["message"]
    assert "Digest:" in row["message"]
    assert "Grazie, ci interessa partecipare." in row["message"]


def test_required_otp_blocks_gmail_provider_before_any_call(monkeypatch, tmp_path):
    store, _, pending = _approval(tmp_path)
    session = FakeSession()

    class WaitingOtp:
        def authorize(self, _approval):
            return {"status": "waiting_for_telegram_otp", "authorized": False}

    monkeypatch.setenv("RALFLOOP_EMAIL_OTP_REQUIRED", "1")
    result = UnifiedGmailApprovalExecutor(
        lambda: Context(GoogleWorkspaceGateway(session, account=ACCOUNT)),
        store=store, account=ACCOUNT, otp_gate=WaitingOtp(),
    ).execute(pending)

    assert result["status"] == "waiting_for_telegram_otp"
    assert result["sent"] is False
    assert session.calls == []


def test_required_otp_authorization_allows_exact_approved_send(monkeypatch, tmp_path):
    store, _, pending = _approval(tmp_path)
    session = FakeSession()

    class AuthorizedOtp:
        def authorize(self, _approval):
            return {"status": "email_otp_authorized", "authorized": True}

    monkeypatch.setenv("RALFLOOP_EMAIL_OTP_REQUIRED", "1")
    gateway = GoogleWorkspaceGateway(session, account=ACCOUNT)
    gateway.discovered_tools = ("manage_email",)
    result = UnifiedGmailApprovalExecutor(
        lambda: Context(gateway), store=store, account=ACCOUNT,
        otp_gate=AuthorizedOtp(),
    ).execute(pending)

    assert result["status"] == "executed"
    assert len([args for _, args in session.calls if args["operation"] == "send"]) == 1


def test_exact_hash_bound_approved_send_is_claimed_and_provider_verified(tmp_path):
    store, _, pending = _approval(tmp_path)
    session = FakeSession()

    result = _executor(store, session).execute(pending)

    assert result["status"] == "executed"
    assert result["provider_message_id"] == "0123456789abcdef"
    assert result["recipient"] == "marco@example.invalid"
    assert result["approved_version"] == pending.version
    assert store.get_request(str(pending.approval_ref))["status"] == "consumed"
    writes = [args for _, args in session.calls if args["operation"] == "send"]
    assert writes == [{
        "operation": "send", "email": ACCOUNT, "to": "marco@example.invalid",
        "subject": "Documenti", "body": "Grazie, documenti ricevuti.",
    }]


def test_reply_preserves_observed_source_message_and_thread(tmp_path):
    store, _, pending = _approval(tmp_path, reply=True)
    session = FakeSession()

    result = _executor(store, session).execute(pending)

    assert result["status"] == "executed"
    assert [args["operation"] for _, args in session.calls] == ["read", "getThread", "reply"]
    assert session.calls[-1][1]["messageId"] == "1234567890abcdef"
    assert result["thread_id"] == "fedcba9876543210"


def test_real_mcp_markdown_reply_preflight_claim_and_confirmation(monkeypatch, tmp_path):
    store, _, pending = _approval(tmp_path, reply=True)
    session = FakeSession(real_markdown=True)
    order = []

    class OneShotOtp:
        def authorize(self, _approval):
            order.append("otp_consumed")
            return {"status": "email_otp_authorized", "authorized": True}

    monkeypatch.setenv("RALFLOOP_EMAIL_OTP_REQUIRED", "1")
    gateway = GoogleWorkspaceGateway(session, account=ACCOUNT)
    gateway.discovered_tools = ("manage_email",)
    result = UnifiedGmailApprovalExecutor(
        lambda: Context(gateway), store=store, account=ACCOUNT, otp_gate=OneShotOtp(),
    ).execute(pending)

    operations = [args["operation"] for _, args in session.calls]
    assert operations == ["read", "getThread", "reply"]
    assert order == ["otp_consumed"]
    assert result["status"] == "executed"
    assert result["provider_message_id"] == "0123456789abcdef"
    assert store.get_request(str(pending.approval_ref))["status"] == "consumed"
    with store.connect() as conn:
        rows = conn.execute(
            "select status,result_json from approval_executions where request_id=? order by execution_id",
            (pending.approval_ref,),
        ).fetchall()
    assert [row["status"] for row in rows] == ["execution_started", "consumed"]
    assert __import__("json").loads(rows[-1]["result_json"])["provider_message_id"] == "0123456789abcdef"


def test_reply_preflight_accepts_account_scoped_alias_or_bcc_message(tmp_path):
    store, _, pending = _approval(tmp_path, reply=True)
    session = FakeSession(source_to="Production alias <production@example.invalid>")

    result = _executor(store, session).execute(pending)

    assert result["status"] == "executed"
    assert [args["operation"] for _, args in session.calls] == ["read", "getThread", "reply"]


def test_provider_preflight_failure_does_not_consume_otp_or_claim(monkeypatch, tmp_path):
    store, _, pending = _approval(tmp_path, reply=True)
    session = FakeSession()
    otp_calls = []

    def fail_preflight(operation, **arguments):
        session.calls.append(("manage_email", {"operation": operation, **arguments}))
        raise OSError("read_provider_down")

    class Otp:
        def authorize(self, _approval):
            otp_calls.append("authorize")
            return {"status": "email_otp_authorized", "authorized": True}

    monkeypatch.setenv("RALFLOOP_EMAIL_OTP_REQUIRED", "1")
    gateway = GoogleWorkspaceGateway(session, account=ACCOUNT)
    gateway.discovered_tools = ("manage_email",)
    gateway.invoke = fail_preflight
    result = UnifiedGmailApprovalExecutor(
        lambda: Context(gateway), store=store, account=ACCOUNT, otp_gate=Otp(),
    ).execute(pending)

    assert result == {
        "status": "provider_preflight_failed", "sent": False,
        "retry_allowed": True, "provider_call_attempted": False,
    }
    assert otp_calls == []
    assert store.get_request(str(pending.approval_ref))["status"] == "approved"
    with store.connect() as conn:
        assert conn.execute(
            "select count(*) from approval_executions where request_id=?",
            (pending.approval_ref,),
        ).fetchone()[0] == 0
    assert not any(args["operation"] in {"send", "reply"} for _, args in session.calls)


def test_real_mcp_ambiguous_response_after_send_blocks_replay(tmp_path):
    store, _, pending = _approval(tmp_path)
    session = FakeSession(unverified=True)
    executor = _executor(store, session)

    first = executor.execute(pending)
    second = executor.execute(pending)

    assert first["status"] == "approved_but_send_failed"
    assert first["reason"] == "provider_confirmation_missing"
    assert second["status"] == "approved_but_send_failed"
    assert len([args for _, args in session.calls if args["operation"] == "send"]) == 1


@pytest.mark.parametrize("status", ["stale", "expired"])
def test_stale_or_expired_approval_never_reaches_provider(tmp_path, status):
    store, _, pending = _approval(tmp_path)
    with store.connect() as conn:
        conn.execute(
            "update approval_requests set status=? where request_id=?",
            (status, pending.approval_ref),
        )
    session = FakeSession()

    result = _executor(store, session).execute(pending)

    assert result["sent"] is False
    assert session.calls == []


def test_provider_failure_marks_execution_failed_and_blocks_blind_retry(tmp_path):
    store, _, pending = _approval(tmp_path)
    session = FakeSession(fail_write=True)
    executor = _executor(store, session)

    first = executor.execute(pending)
    second = executor.execute(pending)

    assert first["status"] == "approved_but_send_failed"
    assert first["retry_allowed"] is False
    assert second["status"] == "approved_but_send_failed"
    assert store.get_request(str(pending.approval_ref))["status"] == "execution_failed"
    assert len([args for _, args in session.calls if args["operation"] == "send"]) == 1


def test_http_like_success_without_message_confirmation_is_fail_closed(tmp_path):
    store, _, pending = _approval(tmp_path)
    session = FakeSession(unverified=True)

    result = _executor(store, session).execute(pending)

    assert result["status"] == "approved_but_send_failed"
    assert result["reason"] == "provider_confirmation_missing"
    assert store.get_request(str(pending.approval_ref))["status"] == "execution_failed"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("recipient", "other@example.invalid"),
        ("subject", "Changed subject"),
        ("body", "Changed body"),
        ("thread_id", "0000000000000000"),
    ),
)
def test_changed_payload_cannot_use_old_approval(tmp_path, field, value):
    store, _, pending = _approval(tmp_path, reply=field == "thread_id")
    changed = pending.model_copy(update={"payload": {**pending.payload, field: value}})
    session = FakeSession()

    result = _executor(store, session).execute(changed)

    assert result["status"] == "approval_required"
    assert session.calls == []


def test_changed_account_cannot_use_old_approval(tmp_path):
    store, _, pending = _approval(tmp_path)
    session = FakeSession()

    result = _executor(store, session, account="other@example.invalid").execute(pending)

    assert result["status"] == "execution_precondition_failed"
    assert result["sent"] is False
    assert session.calls == []
    assert store.get_request(str(pending.approval_ref))["status"] == "stale"


def test_mcp_unavailable_fails_closed_before_send(tmp_path):
    store, _, pending = _approval(tmp_path)

    class UnavailableContext(AbstractContextManager):
        def __enter__(self):
            raise OSError("mcp_unavailable")

        def __exit__(self, *_):
            return None

    executor = UnifiedGmailApprovalExecutor(
        lambda: UnavailableContext(), store=store, account=ACCOUNT
    )
    result = executor.execute(pending)

    assert result["status"] == "provider_preflight_failed"
    assert result["retry_allowed"] is True
    assert result["provider_call_attempted"] is False
    assert store.get_request(str(pending.approval_ref))["status"] == "approved"


def test_mcp_ok_false_is_real_failure_not_success(tmp_path):
    store, _, pending = _approval(tmp_path)

    result = _executor(store, FakeSession(ok_false=True)).execute(pending)

    assert result["status"] == "approved_but_send_failed"
    assert result["sent"] is False
    assert result["retry_allowed"] is False
    assert store.get_request(str(pending.approval_ref))["status"] == "execution_failed"


def test_crash_restart_executing_claim_never_replays_provider_call(tmp_path):
    store, _, pending = _approval(tmp_path)
    claimed = store.claim_execution(str(pending.approval_ref), action=pending.action)
    assert claimed["claimed"] is True
    session = FakeSession()

    result = _executor(
        DomainApprovalStore(db_path=store.db_path, policy=store.policy), session
    ).execute(pending)

    assert result["status"] == "send_outcome_pending"
    assert result["retry_allowed"] is False
    assert session.calls == []


def test_reply_provider_wrong_thread_confirmation_is_fail_closed(tmp_path):
    store, _, pending = _approval(tmp_path, reply=True)

    result = _executor(store, FakeSession(wrong_thread=True)).execute(pending)

    assert result["status"] == "approved_but_send_failed"
    assert result["reason"] == "provider_thread_confirmation_mismatch"
    assert store.get_request(str(pending.approval_ref))["status"] == "execution_failed"
