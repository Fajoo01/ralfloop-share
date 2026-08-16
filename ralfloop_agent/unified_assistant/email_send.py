from __future__ import annotations

from contextlib import AbstractContextManager
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains.storage import append_jsonl
from src.google_workspace import GoogleWorkspaceGateway
from src.mcp_transport import MCPClientSession, UnixMCPTransport

from .conversation import PendingAction, approval_matches, payload_matches
from .email_otp import EmailOtpGate


def build_email_approval_scope(pending: PendingAction, *, account: str) -> dict[str, Any]:
    if pending.domain != "email" or pending.action not in {"send_email", "reply_email"}:
        raise ValueError("email_pending_required")
    payload = pending.payload
    artifact = {
        "action": pending.action,
        "version": 1,
        "account": account,
        "draft_id": pending.pending_id,
        "draft_version": pending.version,
        "payload_digest": pending.payload_digest,
        "recipient": str(payload.get("recipient") or ""),
        "subject": str(payload.get("subject") or ""),
        "body": str(payload.get("body") or ""),
        "source_message_id": str(payload.get("source_message_id") or ""),
        "thread_id": str(payload.get("thread_id") or ""),
        "cc": str(payload.get("cc") or ""),
        "bcc": str(payload.get("bcc") or ""),
        "attachments": list(payload.get("attachments") or ()),
        "attachment_paths": list(payload.get("attachment_paths") or ()),
    }
    raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    artifact_sha256 = hashlib.sha256(raw).hexdigest()
    return {
        **artifact,
        "artifact_sha256": artifact_sha256,
        "body_sha256": hashlib.sha256(artifact["body"].encode()).hexdigest(),
        "idempotency_key": "email:" + hashlib.sha256(
            (account.casefold() + "\x00" + artifact_sha256).encode()
        ).hexdigest(),
    }


class UnifiedEmailApprovalCoordinator:
    """Bridges bounded conversation pending state to the existing approval store."""

    def __init__(
        self,
        store: DomainApprovalStore,
        *,
        policy: DomainApprovalPolicy,
        account: str,
        outbox_path: str | Path | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.account = account
        self.outbox_path = Path(outbox_path) if outbox_path else None

    def request(self, pending: PendingAction, *, requested_by: str) -> dict[str, Any]:
        if not self.policy.enabled:
            return {"status": "approval_gate_disabled"}
        if not self.policy.allowed_user_ids or not self.policy.allowed_chat_ids:
            return {"status": "approval_allowlist_unconfigured"}
        scope = build_email_approval_scope(pending, account=self.account)
        created = self.store.create_request(
            action=pending.action,
            bando_id="google_workspace.gmail",
            version=str(pending.version),
            scope=scope,
            requested_by=requested_by,
        )
        request = created.get("request") if isinstance(created, Mapping) else None
        if not isinstance(request, Mapping):
            return {"status": str(created.get("status") or "approval_request_failed")}
        notification_queued = False
        if self.outbox_path is not None:
            telegram_message = str(request.get("telegram_message") or "")
            try:
                append_jsonl(self.outbox_path, {
                    "status": "queued",
                    "request_id": str(request["request_id"]),
                    "api_url": self.policy.api_url,
                    "message": self._notification_preview(pending, telegram_message),
                })
            except OSError:
                self.store.cancel(str(request["request_id"]))
                return {"status": "approval_notification_failed"}
            notification_queued = True
        return {
            "status": "pending",
            "request_id": str(request["request_id"]),
            "created_at": int(request["created_at"]),
            "expires_at": int(request["expires_at"]),
            "scope_digest_short": str(request["scope_digest_short"]),
            "notification_queued": notification_queued,
        }

    @staticmethod
    def _notification_preview(pending: PendingAction, approval_message: str) -> str:
        payload = pending.payload
        return "\n".join((
            approval_message.strip(),
            "",
            f"Bozza per: {str(payload.get('recipient') or '')[:320]}",
            f"Oggetto: {str(payload.get('subject') or '')[:500]}",
            "",
            str(payload.get("body") or "")[:2400],
        )).strip()

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
        scope = build_email_approval_scope(pending, account=self.account)
        if str(row.get("scope_digest") or "") != scope_digest(scope):
            self.store.mark_stale(pending.approval_ref, ["email_artifact_changed"])
            return {"status": "scope_digest_mismatch"}
        decision = DomainApprovalDecision(
            request_id=pending.approval_ref,
            decision="approve",
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id,
            chat_type=chat_type,
            idempotency_key=(
                f"unified-email:{pending.pending_id}:{pending.version}:{telegram_message_id}"
            ),
        )
        return self.store.decide(
            decision, scope_digest_short=str(row.get("scope_digest_short") or "")
        )

    def cancel(self, pending: PendingAction) -> dict[str, Any]:
        if not pending.approval_ref:
            return {"status": "no_approval_request"}
        return self.store.cancel(pending.approval_ref)


class GoogleWorkspaceEmailContext(AbstractContextManager[GoogleWorkspaceGateway]):
    def __init__(self, socket_path: str, account: str, timeout: float) -> None:
        self.socket_path = socket_path
        self.account = account
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    def __enter__(self) -> GoogleWorkspaceGateway:
        self.session = MCPClientSession(UnixMCPTransport(self.socket_path), timeout=self.timeout)
        self.session.__enter__()
        self.gateway = GoogleWorkspaceGateway(self.session, account=self.account)
        self.gateway.discover()
        return self.gateway

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            self.session.__exit__(exc_type, exc, tb)


class UnifiedGmailApprovalExecutor:
    """Uses the existing Gmail provider; no unapproved mutation entrypoint exists."""

    def __init__(
        self,
        gateway_factory: Callable[[], AbstractContextManager[GoogleWorkspaceGateway]],
        *,
        store: DomainApprovalStore,
        account: str,
        otp_gate: EmailOtpGate | None = None,
    ) -> None:
        self.gateway_factory = gateway_factory
        self.store = store
        self.account = account
        self.otp_gate = otp_gate

    @classmethod
    def from_environment(cls, *, store: DomainApprovalStore) -> "UnifiedGmailApprovalExecutor":
        socket_path = os.getenv(
            "RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock"
        )
        account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
        timeout = float(os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "20"))
        return cls(
            lambda: GoogleWorkspaceEmailContext(socket_path, account, timeout),
            store=store,
            account=account,
            otp_gate=EmailOtpGate.from_environment() if EmailOtpGate.required() else None,
        )

    def execute(self, pending: PendingAction) -> dict[str, Any]:
        if not payload_matches(pending) or not approval_matches(pending):
            return {"status": "approval_required", "sent": False}
        if pending.action not in {"send_email", "reply_email"}:
            return {"status": "denied", "sent": False}
        row = self.store.get_request(str(pending.approval_ref))
        stored_status = str((row or {}).get("status") or "")
        if stored_status == "consumed":
            return {"status": "already_executed", "sent": True, "retry_allowed": False}
        if stored_status == "executing":
            return {"status": "send_outcome_pending", "sent": False, "retry_allowed": False}
        if stored_status == "execution_failed":
            return {
                "status": "approved_but_send_failed", "sent": False,
                "retry_allowed": False, "reason": "previous_send_outcome_uncertain",
            }
        if row is None or effective_approval_status(row) != "approved":
            return {
                "status": "approval_expired" if row and int(row.get("expires_at") or 0) <= int(time.time())
                else "approval_not_executable",
                "sent": False, "retry_allowed": False,
            }
        scope = build_email_approval_scope(pending, account=self.account)
        context = self.gateway_factory()
        try:
            gateway = context.__enter__()
        except Exception:
            return {
                "status": "provider_preflight_failed", "sent": False,
                "retry_allowed": True, "provider_call_attempted": False,
            }
        try:
            try:
                gateway.preflight_approved_email(scope)
            except Exception:
                return {
                    "status": "provider_preflight_failed", "sent": False,
                    "retry_allowed": True, "provider_call_attempted": False,
                }
            if EmailOtpGate.required():
                if self.otp_gate is None:
                    return {
                        "status": "email_otp_unavailable", "sent": False,
                        "retry_allowed": False,
                    }
                otp = self.otp_gate.authorize(row)
                if not otp.get("authorized"):
                    return {
                        "status": str(otp.get("status") or "email_otp_required"),
                        "sent": False, "retry_allowed": False,
                    }
            try:
                return dict(gateway.execute_approved_email(
                    self.store, str(pending.approval_ref), scope
                ))
            except Exception:
                failure = {
                    "status": "execution_precondition_failed", "sent": False,
                    "retry_allowed": False, "provider_call_attempted": False,
                }
                row = self.store.get_request(str(pending.approval_ref))
                if row and row.get("status") == "executing":
                    uncertain = {**failure, "status": "approved_but_send_failed",
                                 "reason": "provider_outcome_uncertain"}
                    self.store.finish_claimed_execution(
                        str(pending.approval_ref), action=pending.action,
                        success=False, result=uncertain,
                    )
                    return uncertain
                if row and row.get("status") == "approved":
                    self.store.mark_stale(
                        str(pending.approval_ref), ["execution_precondition_failed_before_claim"]
                    )
                return failure
        finally:
            try:
                context.__exit__(None, None, None)
            except Exception:
                pass
__all__ = [
    "GoogleWorkspaceEmailContext",
    "UnifiedEmailApprovalCoordinator",
    "UnifiedGmailApprovalExecutor",
    "build_email_approval_scope",
]
