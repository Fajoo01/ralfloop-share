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
READ_TOOLS = frozenset({READ_TOOL})
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
        if READ_TOOL not in self.discovered_tools:
            raise MCPProtocolError("arci_tool_not_discovered")
        result = self.session.call_tool(READ_TOOL, {})
        structured = (
            result.get("structuredContent")
            if isinstance(result, Mapping)
            else None
        )
        payload = structured if isinstance(structured, Mapping) else result
        if not isinstance(payload, Mapping):
            raise MCPProtocolError("arci_result_not_object")
        return _validated_payload(payload)


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
    buckets = sum((
        profile.governance_under_15 or 0,
        profile.governance_age_15_30 or 0,
        profile.governance_over_30 or 0,
    ))
    if (
        buckets != profile.governance_dated
        or (profile.governance_dated or 0) > (profile.governance_total or 0)
        or (profile.member_count or 0) < (profile.governance_total or 0)
    ):
        raise MCPProtocolError("arci_result_invalid")

    return {
        "ok": True,
        "operation": "read_organization_profile",
        **profile.model_dump(mode="json"),
        "writes": 0,
        "sends": 0,
    }


__all__ = [
    "ArciGateway",
    "ArciGatewayError",
    "ArciMCPContext",
    "FORBIDDEN_INPUTS",
    "READ_TOOL",
    "READ_TOOLS",
]
