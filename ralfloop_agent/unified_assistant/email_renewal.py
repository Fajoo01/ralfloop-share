from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from ralfloop_agent.cli.session_store import SessionStore
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from .contracts import PolicyClass
from .conversation import PENDING_DOMAINS, SessionConversationAdapter
from .email_reconcile import EmailReconciler
from .email_send import UnifiedEmailApprovalCoordinator


class EmailApprovalRenewal:
    """Create a fresh hash-bound approval only after deterministic NOT_SENT proof."""

    def __init__(
        self,
        *,
        reconciler: EmailReconciler,
        store: DomainApprovalStore,
        coordinator: UnifiedEmailApprovalCoordinator,
        sessions: SessionConversationAdapter,
    ) -> None:
        self.reconciler = reconciler
        self.store = store
        self.coordinator = coordinator
        self.sessions = sessions

    @classmethod
    def from_environment(cls) -> "EmailApprovalRenewal":
        reconciler = EmailReconciler.from_environment()
        store = reconciler.store
        policy = store.policy
        account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
        sessions = SessionConversationAdapter(SessionStore(os.getenv(
            "RALFLOOP_UNIFIED_SESSION_DIR",
            str(Path.home() / ".local" / "state" / "ralf" / "unified-sessions"),
        )))
        return cls(
            reconciler=reconciler,
            store=store,
            coordinator=UnifiedEmailApprovalCoordinator(
                store, policy=policy, account=account,
                outbox_path=os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX") or None,
            ),
            sessions=sessions,
        )

    def renew(
        self,
        *,
        previous_approval_id: str,
        previous_otp_request_id: str,
        thread_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        proof = self.reconciler.reconcile(
            approval_id=previous_approval_id,
            otp_request_id=previous_otp_request_id,
            thread_id=thread_id,
        )
        if proof.final != "NOT_SENT_CONFIRMED":
            return {"status": "renewal_denied", "reason": proof.final}
        previous = self.store.get_request(previous_approval_id)
        if not previous or str(previous.get("status") or "") not in {"stale", "expired"}:
            return {"status": "renewal_denied", "reason": "previous_approval_not_final"}
        scope = previous.get("scope") or {}
        if (
            str(scope.get("thread_id") or "") != thread_id
            or str(scope.get("action") or "") != "reply_email"
            or hashlib.sha256(str(scope.get("body") or "").encode()).hexdigest()
            != str(scope.get("body_sha256") or "")
        ):
            return {"status": "renewal_denied", "reason": "immutable_scope_invalid"}
        conversation = self.sessions.load(session_id)
        active = [
            item for name in PENDING_DOMAINS
            if (item := getattr(conversation.state.pending, name)) is not None
        ]
        if active:
            return {"status": "renewal_denied", "reason": "session_has_pending_action"}
        payload = {
            "recipient": str(scope.get("recipient") or ""),
            "subject": str(scope.get("subject") or ""),
            "body": str(scope.get("body") or ""),
            "source_message_id": str(scope.get("source_message_id") or ""),
            "thread_id": thread_id,
            "reply_mode": True,
            "risk": "high",
            "validation_state": "passed",
            "approval_action": "reply_email",
        }
        preview = "\n".join((
            f"Bozza per: {payload['recipient']}",
            f"Oggetto: {payload['subject']}", "", payload["body"], "", "Invio?",
        ))
        pending = conversation.stage(
            domain="email", action="reply_email", policy=PolicyClass.CONFIRM_WRITE,
            payload=payload, displayed_text=preview,
            ttl_seconds=self.coordinator.policy.ttl_sec,
        )
        created = self.coordinator.request(
            pending, requested_by=f"email-renewal:{previous_approval_id}"
        )
        if created.get("status") != "pending":
            conversation.clear("email")
            return {"status": "renewal_failed", "reason": str(created.get("status") or "")}
        pending = conversation.attach_approval_request(
            domain="email", pending_id=pending.pending_id,
            payload_digest=pending.payload_digest,
            approval_ref=str(created["request_id"]),
            created_at=int(created["created_at"]), expires_at=int(created["expires_at"]),
        )
        self.sessions.save(session_id, conversation)
        return {
            "status": "pending",
            "request_id": str(created["request_id"]),
            "scope_digest_short": str(created["scope_digest_short"]),
            "pending_id": pending.pending_id,
            "notification_queued": bool(created.get("notification_queued")),
            "otp_created": False,
        }


__all__ = ["EmailApprovalRenewal"]
