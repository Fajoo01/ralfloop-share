from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
from typing import Any, Callable, Mapping

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from src.whatsapp import WhatsAppGateway, WhatsAppMCPContext

from .conversation import PendingAction, approval_matches, payload_matches


def build_whatsapp_approval_scope(pending: PendingAction) -> dict[str, Any]:
    if pending.domain != "whatsapp" or pending.action not in {
        "whatsapp_send", "whatsapp_reply",
    }:
        raise ValueError("whatsapp_pending_required")
    payload = pending.payload
    artifact = {
        "action": pending.action,
        "version": 1,
        "draft_id": pending.pending_id,
        "draft_version": pending.version,
        "payload_digest": pending.payload_digest,
        "chat_id": str(payload.get("chat_id") or ""),
        "chat_title": str(payload.get("chat_title") or payload.get("recipient") or ""),
        "reply_to_message_id": str(payload.get("reply_to_message_id") or ""),
        "recipient": str(payload.get("recipient") or ""),
        "body": str(payload.get("body") or ""),
        "risk": str(payload.get("risk") or "normal"),
        "validation_state": str(payload.get("validation_state") or ""),
    }
    raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(raw).hexdigest()
    return {
        **artifact,
        "artifact_sha256": digest,
        "body_sha256": hashlib.sha256(artifact["body"].encode()).hexdigest(),
        "execution_id": "waexec_" + digest[:24],
    }


class UnifiedWhatsAppApprovalCoordinator:
    def __init__(self, store: DomainApprovalStore, *, policy: DomainApprovalPolicy) -> None:
        self.store = store
        self.policy = policy

    def request(self, pending: PendingAction, *, requested_by: str) -> dict[str, Any]:
        if not self.policy.enabled:
            return {"status": "approval_gate_disabled"}
        if not self.policy.allowed_user_ids or not self.policy.allowed_chat_ids:
            return {"status": "approval_allowlist_unconfigured"}
        scope = build_whatsapp_approval_scope(pending)
        created = self.store.create_request(
            action=pending.action, bando_id="whatsapp.web.work",
            version=str(pending.version), scope=scope, requested_by=requested_by,
        )
        request = created.get("request") if isinstance(created, Mapping) else None
        if not isinstance(request, Mapping):
            return {"status": str(created.get("status") or "approval_request_failed")}
        return {
            "status": "pending", "request_id": str(request["request_id"]),
            "created_at": int(request["created_at"]),
            "expires_at": int(request["expires_at"]),
            "scope_digest_short": str(request["scope_digest_short"]),
        }

    def approve(
        self,
        pending: PendingAction,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        chat_type: str = "private",
    ) -> dict[str, Any]:
        if not pending.approval_ref:
            return {"status": "approval_request_missing"}
        row = self.store.get_request(pending.approval_ref)
        if not row:
            return {"status": "approval_request_missing"}
        scope = build_whatsapp_approval_scope(pending)
        if str(row.get("scope_digest") or "") != scope_digest(scope):
            self.store.mark_stale(pending.approval_ref, ["whatsapp_artifact_changed"])
            return {"status": "scope_digest_mismatch"}
        return self.store.decide(
            DomainApprovalDecision(
                request_id=pending.approval_ref, decision="approve",
                telegram_user_id=telegram_user_id, telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id, chat_type=chat_type,
                idempotency_key=(
                    f"unified-whatsapp:{pending.pending_id}:{pending.version}:"
                    f"{telegram_message_id}"
                ),
            ),
            scope_digest_short=str(row.get("scope_digest_short") or ""),
        )

    def cancel(self, pending: PendingAction) -> dict[str, Any]:
        return (
            self.store.cancel(pending.approval_ref)
            if pending.approval_ref else {"status": "no_approval_request"}
        )


class UnifiedWhatsAppApprovalExecutor:
    def __init__(
        self,
        gateway_factory: Callable[[], AbstractContextManager[WhatsAppGateway]],
        *,
        store: DomainApprovalStore,
    ) -> None:
        self.gateway_factory = gateway_factory
        self.store = store

    @classmethod
    def from_environment(
        cls, *, store: DomainApprovalStore,
    ) -> "UnifiedWhatsAppApprovalExecutor":
        return cls(WhatsAppMCPContext.from_environment, store=store)

    def execute(self, pending: PendingAction) -> dict[str, Any]:
        if not payload_matches(pending) or not approval_matches(pending):
            return {"status": "APPROVAL_REQUIRED", "sent": False}
        row = self.store.get_request(str(pending.approval_ref))
        stored = str((row or {}).get("status") or "")
        if stored == "consumed":
            return {"status": "already_executed", "sent": True, "retry_allowed": False}
        if stored in {"executing", "execution_failed"}:
            return {"status": "EXECUTION_UNCERTAIN", "sent": False, "retry_allowed": False}
        scope = build_whatsapp_approval_scope(pending)
        try:
            with self.gateway_factory() as gateway:
                return dict(gateway.execute_approved(
                    self.store, str(pending.approval_ref), scope,
                ))
        except Exception:
            row = self.store.get_request(str(pending.approval_ref))
            if row and row.get("status") == "executing":
                failure = {
                    "status": "EXECUTION_UNCERTAIN", "sent": False,
                    "verified": False, "retry_allowed": False,
                }
                self.store.finish_claimed_execution(
                    str(pending.approval_ref), action=pending.action,
                    success=False, result=failure,
                )
                return failure
            return {"status": "SOURCE_UNAVAILABLE", "sent": False, "retry_allowed": False}


__all__ = [
    "UnifiedWhatsAppApprovalCoordinator", "UnifiedWhatsAppApprovalExecutor",
    "build_whatsapp_approval_scope",
]
