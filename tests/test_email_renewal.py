from __future__ import annotations

import hashlib
import json

from ralfloop_agent.cli.session_store import SessionStore
from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.conversation import SessionConversationAdapter
from ralfloop_agent.unified_assistant.email_renewal import EmailApprovalRenewal
from ralfloop_agent.unified_assistant.email_send import UnifiedEmailApprovalCoordinator


class Proof:
    def __init__(self, final="NOT_SENT_CONFIRMED"):
        self.final = final


class Reconciler:
    def __init__(self, final="NOT_SENT_CONFIRMED"):
        self.final = final
        self.calls = []

    def reconcile(self, **kwargs):
        self.calls.append(kwargs)
        return Proof(self.final)


def _fixture(tmp_path, final="NOT_SENT_CONFIRMED"):
    policy = DomainApprovalPolicy(
        enabled=True, ttl_sec=300, max_pending=10,
        allowed_user_ids={11}, allowed_chat_ids={22},
        db_path=str(tmp_path / "approvals.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    store = DomainApprovalStore(policy=policy)
    body = "Grazie. Ci interessa partecipare; il ruolo resta da definire."
    scope = {
        "action": "reply_email", "account": "fabio@tiremminnanz.com",
        "recipient": "caterina@circolomagnolia.it", "subject": "Magnolia",
        "body": body, "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "source_message_id": "19fd1fbc9ff936d0",
        "thread_id": "19fd1fbc9ff936d0",
    }
    previous = store.create_request(
        action="reply_email", bando_id="google_workspace.gmail", version="1",
        scope=scope, requested_by="test",
    )["request"]
    with store.connect() as conn:
        conn.execute(
            "update approval_requests set status='stale' where request_id=?",
            (previous["request_id"],),
        )
    session_store = SessionStore(tmp_path / "sessions")
    session_store.save({
        "session_id": "telegram-11-11", "cwd": str(tmp_path), "history": [],
        "metadata": {}, "context_enabled": True,
    })
    adapter = SessionConversationAdapter(session_store)
    outbox = tmp_path / "outbox.jsonl"
    renewal = EmailApprovalRenewal(
        reconciler=Reconciler(final), store=store,
        coordinator=UnifiedEmailApprovalCoordinator(
            store, policy=policy, account="fabio@tiremminnanz.com", outbox_path=outbox,
        ),
        sessions=adapter,
    )
    return renewal, store, adapter, outbox, previous["request_id"]


def test_not_sent_creates_fresh_pending_hash_bound_approval_without_otp(tmp_path):
    renewal, store, sessions, outbox, previous_id = _fixture(tmp_path)

    result = renewal.renew(
        previous_approval_id=previous_id, previous_otp_request_id="mailotp_old",
        thread_id="19fd1fbc9ff936d0", session_id="telegram-11-11",
    )

    assert result["status"] == "pending"
    assert result["request_id"] != previous_id
    assert result["otp_created"] is False
    new = store.get_request(result["request_id"])
    assert new["status"] == "pending"
    assert new["scope"]["recipient"] == "caterina@circolomagnolia.it"
    assert new["scope"]["thread_id"] == "19fd1fbc9ff936d0"
    assert store.get_request(previous_id)["status"] == "stale"
    pending = sessions.load("telegram-11-11").state.pending.email
    assert pending is not None and pending.approval_ref == result["request_id"]
    queued = json.loads(outbox.read_text(encoding="utf-8"))
    assert queued["request_id"] == result["request_id"]
    assert "caterina@circolomagnolia.it" in queued["message"]


def test_ambiguous_proof_cannot_create_approval(tmp_path):
    renewal, store, sessions, outbox, previous_id = _fixture(tmp_path, "AMBIGUOUS")

    result = renewal.renew(
        previous_approval_id=previous_id, previous_otp_request_id="mailotp_old",
        thread_id="19fd1fbc9ff936d0", session_id="telegram-11-11",
    )

    assert result == {"status": "renewal_denied", "reason": "AMBIGUOUS"}
    assert store.count_pending() == 0
    assert sessions.load("telegram-11-11").state.pending.email is None
    assert not outbox.exists()
