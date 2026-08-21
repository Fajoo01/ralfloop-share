from __future__ import annotations

"""ARCI read-only portal adapter.

Espone login_status e organization_profile tramite il gateway MCP ARCI.
Qualsiasi operazione di scrittura viene rifiutata in modo fail-closed.
"""

from typing import Any, Mapping, Sequence

from src.portal_adapter import ApprovalPortalService, PortalAdapter


class ArciPortalAdapter:
    def __init__(self, gateway: Any) -> None:
        self.gateway = gateway

    def snapshot(self) -> Mapping[str, Any]:
        profile = self.organization_profile()
        return {
            "status": "OK",
            "operation": "arci.organization_profile.read",
            "profile": profile,
            "writes": 0,
            "sends": 0,
            "approval_required": False,
            "executed": False,
        }

    def preview(
        self,
        operations: Sequence[Mapping[str, Any]],
    ) -> Mapping[str, Any]:
        return {
            "status": "READ_ONLY_PORTAL",
            "operation_count": len(list(operations)),
            "writes": 0,
            "sends": 0,
            "approval_required": False,
            "executed": False,
            "reason": "arci_portal_is_read_only",
        }

    def request(
        self,
        operations: Sequence[Mapping[str, Any]],
        requested_by: str = "ralf",
    ) -> Mapping[str, Any]:
        return {
            "status": "REJECTED_READ_ONLY",
            "requested_by": requested_by,
            "operation_count": len(list(operations)),
            "writes": 0,
            "sends": 0,
            "approval_required": False,
            "executed": False,
            "reason": "arci_portal_does_not_accept_write_requests",
        }

    def execute(self, request_id: str) -> Mapping[str, Any]:
        return {
            "status": "REJECTED_READ_ONLY",
            "request_id": request_id,
            "writes": 0,
            "sends": 0,
            "approval_required": False,
            "executed": False,
            "reason": "arci_portal_does_not_accept_write_requests",
        }

    def login_status(self) -> Mapping[str, Any]:
        try:
            profile = self.organization_profile()
        except Exception as exc:
            return {
                "status": "UNAVAILABLE",
                "authenticated": False,
                "reason": type(exc).__name__,
            }

        return {
            "status": "OK",
            "authenticated": bool(
                profile.get("session_authenticated", False)
            ),
            "profile_status": profile.get("status", "UNKNOWN"),
            "organization_name": profile.get("organization_name"),
            "member_count": profile.get("member_count"),
        }

    def organization_profile(self) -> Mapping[str, Any]:
        try:
            profile = self.gateway.read_organization_profile()
        except Exception as exc:
            return {
                "status": "UNAVAILABLE",
                "session_authenticated": False,
                "read_operations": [],
                "write_operations": 0,
                "side_effects": 0,
                "writes": 0,
                "sends": 0,
                "reason": type(exc).__name__,
            }
        return profile


def make_arci_portal_service(
    gateway: Any,
    *,
    audit_path: str | None = None,
) -> ApprovalPortalService:
    return ApprovalPortalService(
        ArciPortalAdapter(gateway),
        audit_path=audit_path,
    )


__all__ = [
    "ArciPortalAdapter",
    "make_arci_portal_service",
]
