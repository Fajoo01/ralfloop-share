from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from typing import Literal

ApprovalChannel = Literal["telegram", "whatsapp"]


@dataclass(frozen=True)
class PrintApproval:
    job_digest: str
    approved_by: str
    channel: ApprovalChannel
    approved_at: str
    reference: str


def print_job_digest(
    *,
    document_id: str,
    printer: str,
    copies: int,
    options: dict | None = None,
) -> str:
    payload = f"{document_id}\0{printer}\0{copies}\0{sorted((options or {}).items())}"
    return sha256(payload.encode("utf-8")).hexdigest()


def validate_print_approval(
    approval: PrintApproval | None,
    *,
    expected_digest: str,
    fabio_identities: set[str],
    max_age_seconds: int = 3600,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Fail closed unless Fabio approved this exact print job via Telegram/WhatsApp."""
    if approval is None:
        return False, "missing_approval"
    if approval.channel not in {"telegram", "whatsapp"}:
        return False, "channel_not_allowed"
    if approval.approved_by not in fabio_identities:
        return False, "approver_not_fabio"
    if approval.job_digest != expected_digest:
        return False, "approval_job_mismatch"
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


def require_print_approval(*args, **kwargs) -> None:
    ok, reason = validate_print_approval(*args, **kwargs)
    if not ok:
        raise PermissionError(f"print blocked: {reason}")
