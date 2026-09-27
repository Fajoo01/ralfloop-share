from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Literal

ApprovalChannel = Literal["telegram", "whatsapp"]
ActionType = Literal["print", "email", "message"]


@dataclass(frozen=True)
class OutboundApproval:
    action_digest: str
    approved_by: str
    channel: ApprovalChannel
    approved_at: str
    reference: str


def _digest(action_type: ActionType, payload: dict) -> str:
    canonical = json.dumps(
        {"action_type": action_type, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def print_action_digest(*, document_id: str, printer: str, copies: int, options: dict | None = None) -> str:
    return _digest("print", {
        "document_id": document_id,
        "printer": printer,
        "copies": copies,
        "options": options or {},
    })


def email_action_digest(*, recipients: list[str] | tuple[str, ...], subject: str, body: str, attachments: list[str] | tuple[str, ...] = ()) -> str:
    return _digest("email", {
        "recipients": list(recipients),
        "subject": subject,
        "body": body,
        "attachments": list(attachments),
    })


def message_action_digest(*, channel: str, recipients: list[str] | tuple[str, ...], body: str, attachments: list[str] | tuple[str, ...] = ()) -> str:
    return _digest("message", {
        "channel": channel,
        "recipients": list(recipients),
        "body": body,
        "attachments": list(attachments),
    })


def validate_outbound_approval(
    approval: OutboundApproval | None,
    *,
    expected_digest: str,
    fabio_identities: set[str],
    max_age_seconds: int = 3600,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Fail closed unless Fabio approved this exact outbound action via Telegram/WhatsApp."""
    if approval is None:
        return False, "missing_approval"
    if approval.channel not in {"telegram", "whatsapp"}:
        return False, "channel_not_allowed"
    if approval.approved_by not in fabio_identities:
        return False, "approver_not_fabio"
    if approval.action_digest != expected_digest:
        return False, "approval_action_mismatch"
    if not approval.reference.strip():
        return False, "missing_approval_reference"

    try:
        approved_at = datetime.fromisoformat(approval.approved_at.replace("Z", "+00:00"))
    except ValueError:
        return False, "invalid_timestamp"
    if approved_at.tzinfo is None:
        approved_at = approved_at.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    age = (current - approved_at.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age > max_age_seconds:
        return False, "approval_stale"
    return True, "approved"


def require_outbound_approval(*args, **kwargs) -> None:
    ok, reason = validate_outbound_approval(*args, **kwargs)
    if not ok:
        raise PermissionError(f"outbound action blocked: {reason}")
