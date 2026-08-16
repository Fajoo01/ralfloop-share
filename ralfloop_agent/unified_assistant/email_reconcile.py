from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from email.utils import getaddresses, parsedate_to_datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import unicodedata
from typing import Any, Callable, Mapping

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.email_search import (
    GmailReadGateway,
    GoogleWorkspaceReadContext,
)
from src.mcp_transport import MCPError


FINAL_STATES = frozenset({"SENT_CONFIRMED", "NOT_SENT_CONFIRMED", "AMBIGUOUS"})
_MESSAGE_ID = re.compile(r"^[0-9a-f]{16,256}$", re.IGNORECASE)


@dataclass(frozen=True)
class EmailReconcileResult:
    final: str
    provider_message_id: str
    approval_status: str
    otp_status: str
    execution_count: int
    send_calls_during_reconcile: int = 0

    def lines(self) -> str:
        return "\n".join((
            f"FINAL={self.final}",
            f"provider_message_id={self.provider_message_id}",
            f"approval_status={self.approval_status}",
            f"otp_status={self.otp_status}",
            f"execution_count={self.execution_count}",
            "send_calls_during_reconcile=0",
        ))


class EmailReconciler:
    """Provider-read-only reconciliation. It has no send or OTP mutation path."""

    def __init__(
        self,
        *,
        store: DomainApprovalStore,
        gateway_factory: Callable[[str], AbstractContextManager[GmailReadGateway]],
        otp_db_path: str | Path,
        otp_binding_db_path: str | Path,
        otp_audit_path: str | Path,
    ) -> None:
        self.store = store
        self.gateway_factory = gateway_factory
        self.otp_db_path = Path(otp_db_path)
        self.otp_binding_db_path = Path(otp_binding_db_path)
        self.otp_audit_path = Path(otp_audit_path)

    @classmethod
    def from_environment(cls) -> "EmailReconciler":
        approval_db = os.getenv(
            "RALFLOOP_TELEGRAM_APPROVAL_DB", "/var/lib/ralfloop/domain-approvals.sqlite3"
        )
        policy = DomainApprovalPolicy.from_env()
        policy.db_path = approval_db
        socket_path = os.getenv(
            "RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock"
        )
        timeout = float(os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "30"))
        return cls(
            store=DomainApprovalStore(policy=policy),
            gateway_factory=lambda account: GoogleWorkspaceReadContext(
                socket_path, account, timeout
            ),
            otp_db_path=os.getenv(
                "RALFLOOP_EMAIL_OTP_DB", "/var/lib/ralfloop/email-otp.sqlite3"
            ),
            otp_binding_db_path=os.getenv(
                "RALFLOOP_EMAIL_OTP_BINDING_DB",
                "/var/lib/ralfloop/email-otp-bindings.sqlite3",
            ),
            otp_audit_path=os.getenv(
                "RALFLOOP_EMAIL_OTP_AUDIT", "/var/lib/ralfloop/email-otp-audit.jsonl"
            ),
        )

    def reconcile(
        self, *, approval_id: str, otp_request_id: str, thread_id: str
    ) -> EmailReconcileResult:
        snapshot = self.store.reconciliation_snapshot(approval_id)
        request = snapshot.get("request")
        executions = list(snapshot.get("executions") or ())
        approval_status = str((request or {}).get("status") or "missing")
        otp = self._otp_snapshot(approval_id, otp_request_id)
        otp_status = str(otp.get("status") or "missing")
        base = {
            "provider_message_id": "", "approval_status": approval_status,
            "otp_status": otp_status, "execution_count": len(executions),
        }
        local = self._validate_local(
            snapshot, otp, approval_id=approval_id,
            otp_request_id=otp_request_id, thread_id=thread_id,
        )
        if local is None:
            return EmailReconcileResult(final="AMBIGUOUS", **base)
        scope, authorized_at = local
        try:
            evidence = self._provider_evidence(
                scope=scope, thread_id=thread_id, authorized_at=authorized_at
            )
        except (MCPError, OSError, TimeoutError, ValueError, RuntimeError):
            return EmailReconcileResult(final="AMBIGUOUS", **base)
        final = str(evidence.get("final") or "AMBIGUOUS")
        if final not in FINAL_STATES:
            final = "AMBIGUOUS"
        if final != "SENT_CONFIRMED":
            return EmailReconcileResult(final=final, **base)
        message_id = str(evidence.get("provider_message_id") or "")
        reconciled = self.store.reconcile_confirmed_execution(
            approval_id,
            action=str(scope["action"]),
            evidence={
                "provider_message_id": message_id,
                "provider_thread_id": thread_id,
                "provider_timestamp": str(evidence.get("provider_timestamp") or ""),
                "recipient": str(scope["recipient"]),
                "subject_sha256": hashlib.sha256(
                    str(scope["subject"]).encode("utf-8")
                ).hexdigest(),
                "body_sha256": str(scope["body_sha256"]),
                "otp_request_id": otp_request_id,
            },
        )
        if not reconciled.get("reconciled"):
            return EmailReconcileResult(final="AMBIGUOUS", **base)
        return EmailReconcileResult(
            final="SENT_CONFIRMED",
            provider_message_id=message_id,
            approval_status=str(reconciled.get("approval_status") or "consumed"),
            otp_status=otp_status,
            execution_count=int(reconciled.get("execution_count") or len(executions)),
        )

    def _otp_snapshot(self, approval_id: str, otp_request_id: str) -> dict[str, Any]:
        try:
            with _read_only_sqlite(self.otp_binding_db_path) as conn:
                binding = conn.execute(
                    "select * from email_otp_bindings "
                    "where approval_request_id = ? and otp_request_id = ?",
                    (approval_id, otp_request_id),
                ).fetchone()
            with _read_only_sqlite(self.otp_db_path) as conn:
                request = conn.execute(
                    "select request_id, created_at, expires_at, status, attempts, "
                    "max_attempts, scope_json, scope_digest, approved_at, consumed_at "
                    "from email_otp_requests where request_id = ?", (otp_request_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            return {"status": "unavailable"}
        if binding is None or request is None:
            return {"status": "missing"}
        audit_events: list[dict[str, Any]] = []
        try:
            with self.otp_audit_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    event = _json_object(line)
                    if str(event.get("request_id") or "") == otp_request_id:
                        audit_events.append(event)
        except OSError:
            return {"status": "unavailable"}
        return {
            "status": str(request["status"]),
            "binding_status": str(binding["status"]),
            "approval_request_id": str(binding["approval_request_id"]),
            "otp_request_id": str(binding["otp_request_id"]),
            "binding_scope": _json_object(binding["otp_scope_json"]),
            "request_scope": _json_object(request["scope_json"]),
            "approved_at": int(request["approved_at"] or 0),
            "consumed_at": int(request["consumed_at"] or 0),
            "scope_digest": str(request["scope_digest"] or ""),
            "audit_events": audit_events,
        }

    @staticmethod
    def _validate_local(
        snapshot: Mapping[str, Any],
        otp: Mapping[str, Any],
        *,
        approval_id: str,
        otp_request_id: str,
        thread_id: str,
    ) -> tuple[Mapping[str, Any], int] | None:
        request = snapshot.get("request")
        if not isinstance(request, Mapping):
            return None
        scope = request.get("scope")
        if not isinstance(scope, Mapping):
            return None
        if str(request.get("request_id") or "") != approval_id:
            return None
        if str(request.get("action") or "") not in {"send_email", "reply_email"}:
            return None
        if str(scope.get("action") or "") != str(request.get("action") or ""):
            return None
        if str(scope.get("thread_id") or "") != thread_id or not _MESSAGE_ID.fullmatch(thread_id):
            return None
        account = str(scope.get("account") or "")
        recipient = str(scope.get("recipient") or "")
        subject = str(scope.get("subject") or "")
        body = str(scope.get("body") or "")
        if not account or not recipient or not subject or not body:
            return None
        if hashlib.sha256(body.encode("utf-8")).hexdigest() != str(
            scope.get("body_sha256") or ""
        ):
            return None
        decisions = snapshot.get("decisions") or ()
        approved_times = [
            int(row.get("created_at") or 0) for row in decisions
            if isinstance(row, Mapping) and str(row.get("decision") or "") == "approve"
            and str(_json_object(row.get("result_json")).get("status") or "")
            in {"approved", "already_approved"}
        ]
        audit = snapshot.get("audit") or ()
        if not approved_times or not any(
            isinstance(row, Mapping) and row.get("event") == "approval_approved"
            for row in audit
        ):
            return None
        if (
            str(otp.get("approval_request_id") or "") != approval_id
            or str(otp.get("otp_request_id") or "") != otp_request_id
            or str(otp.get("status") or "") != "consumed"
            or str(otp.get("binding_status") or "") != "consumed"
            or int(otp.get("approved_at") or 0) <= 0
            or int(otp.get("consumed_at") or 0) <= 0
        ):
            return None
        binding_scope = otp.get("binding_scope")
        request_scope = otp.get("request_scope")
        if not isinstance(binding_scope, Mapping) or binding_scope != request_scope:
            return None
        recipients = binding_scope.get("to")
        if isinstance(recipients, str):
            recipients = [recipients]
        if not isinstance(recipients, list) or recipient.casefold() not in {
            str(item).casefold() for item in recipients
        }:
            return None
        if str(binding_scope.get("subject") or "") != subject:
            return None
        audit_events = otp.get("audit_events") or ()
        scope_digest = str(otp.get("scope_digest") or "")
        audit_names = {
            str(item.get("event") or "") for item in audit_events
            if isinstance(item, Mapping)
            and str(item.get("scope_digest") or "") == scope_digest
        }
        if not {"otp_requested", "otp_approved", "otp_consumed"} <= audit_names:
            return None
        authorized_at = max(
            max(approved_times), int(otp["approved_at"]), int(otp["consumed_at"])
        )
        return scope, authorized_at

    def _provider_evidence(
        self, *, scope: Mapping[str, Any], thread_id: str, authorized_at: int
    ) -> dict[str, Any]:
        account = str(scope["account"])
        recipient = str(scope["recipient"])
        subject = str(scope["subject"])
        approved_body = str(scope["body"])
        query = (
            f'in:sent to:{_gmail_atom(recipient)} '
            f'subject:"{_gmail_phrase(subject)}" after:{max(1, authorized_at - 1)}'
        )
        with self.gateway_factory(account) as gateway:
            thread = gateway.invoke("getThread", threadId=thread_id)
            search = gateway.invoke("search", query=query, maxResults=100)
            rows = _rows(search, "messages", "results", "emails")
            if _continuation(search) or len(rows) >= 100:
                return {"final": "AMBIGUOUS"}
            thread_messages = _rows(thread, "messages")
            if str(thread.get("threadId") or thread.get("thread_id") or "") != thread_id:
                return {"final": "AMBIGUOUS"}
            near_match = False
            for summary in rows:
                message_id = _first(summary, "messageId", "message_id", "id")
                if not _MESSAGE_ID.fullmatch(message_id):
                    near_match = True
                    continue
                detail = gateway.invoke("read", messageId=message_id)
                message = detail.get("message") if isinstance(detail.get("message"), Mapping) else detail
                if not isinstance(message, Mapping):
                    near_match = True
                    continue
                candidate = {**summary, **message, "messageId": message_id}
                if self._matches(
                    candidate, thread_messages=thread_messages, thread_id=thread_id,
                    account=account, recipient=recipient, subject=subject,
                    approved_body=approved_body, authorized_at=authorized_at,
                ):
                    return {
                        "final": "SENT_CONFIRMED",
                        "provider_message_id": message_id,
                        "provider_timestamp": _first(
                            candidate, "internalDate", "date", "sentAt", "timestamp"
                        ),
                    }
                near_match = True
            outbound = any(
                _same_address(_first(item, "from", "sender"), account)
                and _timestamp(_first(item, "internalDate", "date", "sentAt", "timestamp"))
                >= authorized_at - 5
                for item in thread_messages
            )
            if near_match or outbound:
                return {"final": "AMBIGUOUS"}
            return {"final": "NOT_SENT_CONFIRMED"}

    @staticmethod
    def _matches(
        candidate: Mapping[str, Any], *, thread_messages: list[Mapping[str, Any]],
        thread_id: str, account: str, recipient: str, subject: str,
        approved_body: str, authorized_at: int,
    ) -> bool:
        candidate_thread = _first(candidate, "threadId", "thread_id")
        thread_bound = candidate_thread == thread_id
        if not candidate_thread:
            fingerprint = _fingerprint(candidate)
            thread_bound = bool(fingerprint) and any(
                _fingerprint(item) == fingerprint for item in thread_messages
            )
        body = _first(candidate, "body", "text", "plainText")
        return all((
            thread_bound,
            _same_address(_first(candidate, "from", "sender"), account),
            _contains_address(_first(candidate, "to", "recipient", "recipients"), recipient),
            _normalize_subject(_first(candidate, "subject")) == _normalize_subject(subject),
            _body_compatible(body, approved_body),
            _timestamp(_first(candidate, "internalDate", "date", "sentAt", "timestamp"))
            >= authorized_at - 5,
        ))


def _read_only_sqlite(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _rows(value: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    for key in keys:
        rows = value.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, Mapping)]
    return []


def _first(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if isinstance(item, list):
            item = ", ".join(str(part) for part in item)
        if item is not None and str(item).strip():
            return str(item).strip()
    return ""


def _continuation(value: Mapping[str, Any]) -> str:
    return _first(value, "nextPageToken", "next_page_token", "nextToken", "cursor", "continuation")


def _gmail_atom(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9@._+\-]", "", value)


def _gmail_phrase(value: str) -> str:
    return value.replace("\\", " ").replace('"', " ").replace("\r", " ").replace("\n", " ")


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _normalize_subject(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _body_compatible(candidate: str, approved: str) -> bool:
    left = _normalize_text(candidate)
    right = _normalize_text(approved)
    return bool(right and (left == right or left.startswith(right + "\n")))


def _addresses(value: str) -> set[str]:
    return {address.casefold() for _, address in getaddresses([value]) if address}


def _same_address(value: str, expected: str) -> bool:
    return _addresses(value) == {expected.casefold()}


def _contains_address(value: str, expected: str) -> bool:
    return expected.casefold() in _addresses(value)


def _timestamp(value: str) -> int:
    raw = str(value or "").strip()
    if not raw:
        return 0
    if raw.isdigit():
        number = int(raw)
        return number // 1000 if number > 10_000_000_000 else number
    try:
        parsed = parsedate_to_datetime(raw)
        return int(parsed.timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0


def _fingerprint(message: Mapping[str, Any]) -> tuple[str, str, int, str] | tuple[()]:
    sender = ",".join(sorted(_addresses(_first(message, "from", "sender"))))
    subject = _normalize_subject(_first(message, "subject"))
    timestamp = _timestamp(_first(message, "internalDate", "date", "sentAt", "timestamp"))
    body = _normalize_text(_first(message, "body", "text", "plainText"))
    if not sender or not subject or not timestamp or not body:
        return ()
    return sender, subject, timestamp, hashlib.sha256(body.encode("utf-8")).hexdigest()


__all__ = ["EmailReconcileResult", "EmailReconciler", "FINAL_STATES"]
