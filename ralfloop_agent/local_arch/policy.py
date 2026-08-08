from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import CompactRoute


POLICY_VERSION = "local-arch-policy-v1"
PROTECTED_ACTIONS = frozenset(
    {
        "telegram_send",
        "social_publish",
        "email_send",
        "cloud_upload",
        "public_link",
        "external_api_publish",
    }
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    effect: str
    reason: str
    approval_required: bool


class RalfPolicy:
    """Only Ralf decides authorization; model routes are untrusted proposals."""

    version = POLICY_VERSION

    def evaluate(
        self,
        route: CompactRoute,
        *,
        operation: str,
        approval: Mapping[str, Any] | None = None,
        dry_run: bool = False,
    ) -> PolicyDecision:
        if dry_run:
            return PolicyDecision(True, "dry_run", "NO_EXECUTION", operation in PROTECTED_ACTIONS)
        if route.a == "RJ":
            return PolicyDecision(False, "reject", route.r or "ROUTER_REJECT", False)
        if operation in PROTECTED_ACTIONS:
            if not self._valid_approval(approval, operation):
                return PolicyDecision(False, "ask_approval", "APPROVAL_REQUIRED", True)
        return PolicyDecision(True, "execute", "POLICY_OK", False)

    @staticmethod
    def _valid_approval(approval: Mapping[str, Any] | None, operation: str) -> bool:
        if not approval:
            return False
        # Approval verification is performed by the established Ralf approval
        # subsystem. This boundary accepts only its already-verified envelope.
        return (
            approval.get("authorized") is True
            and approval.get("operation") == operation
            and isinstance(approval.get("binding_hash"), str)
            and len(approval["binding_hash"]) == 64
        )
