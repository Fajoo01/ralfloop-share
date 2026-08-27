from __future__ import annotations

"""Strict read-only ARCI MCP facade. Browser primitives stay server-side."""

from contextlib import AbstractContextManager
import os
from typing import Any, Mapping

from ralfloop_agent.unified_assistant.arci_portal import ArciOrganizationProfile
from src.mcp_transport import (
    MCPClientSession,
    MCPProtocolError,
    UnixMCPTransport,
)


READ_TOOL = "arci_read_organization_profile"
SEMANTIC_TOOLS = {
    READ_TOOL: "read_organization_profile",
    "arci_read_club": "read_club",
    "arci_read_current_cards": "read_current_cards",
    "arci_read_committee": "read_committee",
    "arci_read_regional": "read_regional",
    "arci_read_dashboard_alerts": "read_dashboard_alerts",
}
READ_TOOLS = frozenset(SEMANTIC_TOOLS)
FORBIDDEN_INPUTS = frozenset({
    "selector",
    "css",
    "xpath",
    "javascript",
    "js",
    "cdp",
    "cdp_method",
    "coordinate",
    "coordinates",
    "url",
    "path",
    "query",
})
_OUTPUT_KEYS = frozenset({
    "ok",
    "operation",
    "status",
    "session_authenticated",
    "organization_name",
    "member_count",
    "as_of",
    "governance_total",
    "governance_dated",
    "governance_under_15",
    "governance_age_15_30",
    "governance_over_30",
    "provenance",
    "read_operations",
    "write_operations",
    "side_effects",
    "content_role",
    "writes",
    "sends",
})


class ArciGatewayError(RuntimeError):
    pass


class ArciGateway:
    def __init__(self, session: MCPClientSession) -> None:
        self.session = session
        self.discovered_tools: tuple[str, ...] = ()

    def discover(self) -> tuple[str, ...]:
        tools = self.session.list_tools()
        names = {tool.name for tool in tools}
        missing = READ_TOOLS - names
        if missing:
            raise MCPProtocolError(
                "arci_read_tools_missing:" + ",".join(sorted(missing))
            )

        generic = {
            name
            for name in names
            if any(term in name.casefold() for term in (
                "click",
                "evaluate",
                "navigate",
                "cdp",
                "javascript",
                "selector",
            ))
        }
        if generic:
            raise MCPProtocolError("arci_generic_browser_tool_exposed")
        unexpected = names - READ_TOOLS
        if unexpected:
            raise MCPProtocolError("arci_unexpected_tool_exposed")

        for tool in tools:
            if tool.name not in READ_TOOLS:
                continue
            schema = tool.input_schema
            if (
                schema.get("type") != "object"
                or schema.get("additionalProperties", True) is not False
                or (schema.get("properties") or {}) != {}
                or (schema.get("required") or []) != []
            ):
                raise MCPProtocolError("arci_tool_schema_not_strict")

        self.discovered_tools = tuple(sorted(names & READ_TOOLS))
        return self.discovered_tools

    def read_organization_profile(self) -> Mapping[str, Any]:
        return self._read(READ_TOOL)

    def read_club(self) -> Mapping[str, Any]:
        return self._read("arci_read_club")

    def read_current_cards(self) -> Mapping[str, Any]:
        return self._read("arci_read_current_cards")

    def read_committee(self) -> Mapping[str, Any]:
        return self._read("arci_read_committee")

    def read_regional(self) -> Mapping[str, Any]:
        return self._read("arci_read_regional")

    def read_dashboard_alerts(self) -> Mapping[str, Any]:
        return self._read("arci_read_dashboard_alerts")

    def _read(self, tool: str) -> Mapping[str, Any]:
        if tool not in self.discovered_tools:
            raise MCPProtocolError("arci_tool_not_discovered")
        result = self.session.call_tool(tool, {})
        structured = (
            result.get("structuredContent")
            if isinstance(result, Mapping)
            else None
        )
        payload = structured if isinstance(structured, Mapping) else result
        if not isinstance(payload, Mapping):
            raise MCPProtocolError("arci_result_not_object")
        return _validated_payload(payload) if tool == READ_TOOL else _validated_semantic_payload(tool, payload)


class ArciMCPContext(AbstractContextManager[ArciGateway]):
    def __init__(self, socket_path: str, timeout: float = 20.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "ArciMCPContext":
        return cls(
            os.getenv(
                "RALF_ARCI_MCP_SOCKET",
                "/run/ralf-arci-mcp/mcp.sock",
            ),
            float(os.getenv("RALF_ARCI_MCP_TIMEOUT", "20")),
        )

    def __enter__(self) -> ArciGateway:
        self.session = MCPClientSession(
            UnixMCPTransport(self.socket_path),
            timeout=self.timeout,
        )
        self.session.__enter__()
        gateway = ArciGateway(self.session)
        gateway.discover()
        return gateway

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            self.session.__exit__(exc_type, exc, tb)


def _validated_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if set(payload) - _OUTPUT_KEYS:
        raise MCPProtocolError("arci_result_contains_unapproved_fields")
    if str(payload.get("operation") or "") != "read_organization_profile":
        raise MCPProtocolError("arci_result_operation_invalid")
    if payload.get("ok") is not True:
        raise MCPProtocolError("arci_result_unavailable")
    if any(payload.get(key, 0) not in (0, None) for key in (
        "side_effects",
        "write_operations",
        "writes",
        "sends",
    )):
        raise MCPProtocolError("arci_read_reported_side_effect")

    profile_keys = set(ArciOrganizationProfile.model_fields)
    profile_payload = {
        key: value
        for key, value in payload.items()
        if key in profile_keys
    }
    try:
        profile = ArciOrganizationProfile.model_validate(profile_payload)
    except Exception as exc:
        raise MCPProtocolError("arci_result_invalid") from exc
    if profile.status not in {"FOUND", "PARTIAL"}:
        raise MCPProtocolError("arci_result_unavailable")
    governance_values = (
        profile.governance_total,
        profile.governance_dated,
        profile.governance_under_15,
        profile.governance_age_15_30,
        profile.governance_over_30,
    )
    if not all(value is None for value in governance_values):
        if any(value is None for value in governance_values):
            raise MCPProtocolError("arci_result_invalid")
        buckets = sum((
            profile.governance_under_15,
            profile.governance_age_15_30,
            profile.governance_over_30,
        ))
        if (
            buckets != profile.governance_dated
            or profile.governance_dated > profile.governance_total
            or (profile.member_count or 0) < profile.governance_total
        ):
            raise MCPProtocolError("arci_result_invalid")

    return {
        "ok": True,
        "operation": "read_organization_profile",
        **profile.model_dump(mode="json"),
        "writes": 0,
        "sends": 0,
    }


_COMMON_KEYS = frozenset({
    "ok", "operation", "status", "provenance", "read_operations",
    "write_operations", "side_effects", "content_role", "writes", "sends",
})
_SEMANTIC_OUTPUT_KEYS = {
    "arci_read_club": frozenset({
        "name", "code", "type", "committee_code", "regional_code",
        "validity_year", "manually_disabled", "digitization_enabled",
    }),
    "arci_read_current_cards": frozenset({"count", "cards"}),
    "arci_read_committee": frozenset({"name", "code", "active"}),
    "arci_read_regional": frozenset({"name", "code", "consumer_movement_active"}),
    "arci_read_dashboard_alerts": frozenset({"count", "alerts"}),
}
_CARD_KEYS = frozenset({
    "status", "validity", "expired", "enabled_at", "disabled_at",
    "preregistration", "consumer_movement_status",
})
_ALERT_KEYS = frozenset({"title", "description", "enabled", "updated_at"})


def _validated_semantic_payload(tool: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    expected_operation = SEMANTIC_TOOLS[tool]
    allowed = _COMMON_KEYS | _SEMANTIC_OUTPUT_KEYS[tool]
    if set(payload) - allowed:
        raise MCPProtocolError("arci_result_contains_unapproved_fields")
    if payload.get("ok") is not True or payload.get("status") != "FOUND":
        raise MCPProtocolError("arci_result_unavailable")
    if payload.get("operation") != expected_operation:
        raise MCPProtocolError("arci_result_operation_invalid")
    if any(payload.get(key, 0) not in (0, None) for key in (
        "side_effects", "write_operations", "writes", "sends",
    )):
        raise MCPProtocolError("arci_read_reported_side_effect")
    if payload.get("content_role") != "data":
        raise MCPProtocolError("arci_result_invalid")
    if not isinstance(payload.get("provenance"), list) or not isinstance(payload.get("read_operations"), list):
        raise MCPProtocolError("arci_result_invalid")
    if tool == "arci_read_current_cards":
        rows = payload.get("cards")
        if not isinstance(rows, list) or payload.get("count") != len(rows):
            raise MCPProtocolError("arci_result_invalid")
        if any(not isinstance(row, Mapping) or set(row) - _CARD_KEYS for row in rows):
            raise MCPProtocolError("arci_result_contains_unapproved_fields")
    if tool == "arci_read_dashboard_alerts":
        rows = payload.get("alerts")
        if not isinstance(rows, list) or payload.get("count") != len(rows):
            raise MCPProtocolError("arci_result_invalid")
        if any(not isinstance(row, Mapping) or set(row) - _ALERT_KEYS for row in rows):
            raise MCPProtocolError("arci_result_contains_unapproved_fields")
    return dict(payload)


__all__ = [
    "ArciGateway",
    "ArciGatewayError",
    "ArciMCPContext",
    "FORBIDDEN_INPUTS",
    "READ_TOOL",
    "READ_TOOLS",
    "SEMANTIC_TOOLS",
]
