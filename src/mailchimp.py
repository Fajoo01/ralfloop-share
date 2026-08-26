from __future__ import annotations

"""Strict read-only Mailchimp Marketing MCP facade."""

from contextlib import AbstractContextManager
import os
import re
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport


READ_TOOLS = frozenset({
    "mailchimp_ping",
    "mailchimp_list_audiences",
    "mailchimp_list_campaigns",
    "mailchimp_list_members",
    "mailchimp_list_segments",
    "mailchimp_list_tags",
    "mailchimp_list_member_tags",
})
ALL_TOOLS = READ_TOOLS

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
    "endpoint",
    "method",
})


class MailchimpGatewayError(RuntimeError):
    pass


class MailchimpGateway:
    def __init__(self, session: MCPClientSession) -> None:
        self.session = session
        self.discovered_tools: tuple[str, ...] = ()
        self._schemas: dict[str, Mapping[str, Any]] = {}

    def discover(self) -> tuple[str, ...]:
        tools = self.session.list_tools()
        names = {tool.name for tool in tools}

        missing = READ_TOOLS - names
        if missing:
            raise MCPProtocolError(
                "mailchimp_read_tools_missing:" + ",".join(sorted(missing))
            )

        unexpected = names - READ_TOOLS
        if unexpected:
            raise MCPProtocolError(
                "mailchimp_unexpected_tool_exposed:" + ",".join(sorted(unexpected))
            )

        for tool in tools:
            schema = tool.input_schema

            if schema.get("type") != "object":
                raise MCPProtocolError("mailchimp_tool_schema_not_object")

            if schema.get("additionalProperties", True) is not False:
                raise MCPProtocolError("mailchimp_tool_schema_not_strict")

            properties = set((schema.get("properties") or {}).keys())
            if properties & FORBIDDEN_INPUTS:
                raise MCPProtocolError("mailchimp_unsafe_tool_schema")

            self._schemas[tool.name] = schema

        self.discovered_tools = tuple(sorted(names))
        return self.discovered_tools

    def invoke_read(
        self,
        tool: str,
        **arguments: Any,
    ) -> Mapping[str, Any]:
        if tool not in READ_TOOLS:
            raise MailchimpGatewayError("mailchimp_read_tool_denied")

        return self._call(tool, arguments)

    def _call(
        self,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if tool not in self.discovered_tools:
            raise MCPProtocolError("mailchimp_tool_not_discovered")

        if set(arguments) & FORBIDDEN_INPUTS:
            raise MailchimpGatewayError("mailchimp_unsafe_argument")

        schema = self._schemas.get(tool) or {}
        allowed = set((schema.get("properties") or {}).keys())

        if set(arguments) - allowed:
            raise MailchimpGatewayError(
                "mailchimp_argument_not_allowlisted"
            )

        required = set(schema.get("required") or ())
        if required - set(arguments):
            raise MailchimpGatewayError("mailchimp_required_argument_missing")

        for name, value in arguments.items():
            spec = (schema.get("properties") or {}).get(name) or {}
            if spec.get("type") == "integer":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise MailchimpGatewayError("mailchimp_argument_invalid")
                if value < int(spec.get("minimum", value)) or value > int(spec.get("maximum", value)):
                    raise MailchimpGatewayError("mailchimp_argument_invalid")
            elif spec.get("type") == "string":
                if not isinstance(value, str):
                    raise MailchimpGatewayError("mailchimp_argument_invalid")
                if len(value) < int(spec.get("minLength", 0)) or len(value) > int(spec.get("maxLength", len(value))):
                    raise MailchimpGatewayError("mailchimp_argument_invalid")
                if spec.get("enum") and value not in spec["enum"]:
                    raise MailchimpGatewayError("mailchimp_argument_invalid")
                if spec.get("pattern") and re.fullmatch(str(spec["pattern"]), value) is None:
                    raise MailchimpGatewayError("mailchimp_argument_invalid")

        result = self.session.call_tool(
            tool,
            dict(arguments),
        )

        structured = (
            result.get("structuredContent")
            if isinstance(result, Mapping)
            else None
        )
        payload = (
            structured
            if isinstance(structured, Mapping)
            else result
        )

        if not isinstance(payload, Mapping):
            raise MCPProtocolError(
                "mailchimp_result_not_object"
            )

        for field in ("side_effects", "writes", "sends"):
            value = payload.get(field, 0)
            if value not in {0, None}:
                raise MCPProtocolError(
                    f"mailchimp_read_reported_{field}"
                )

        return dict(payload)


class MailchimpMCPContext(
    AbstractContextManager[MailchimpGateway]
):
    def __init__(
        self,
        socket_path: str,
        timeout: float = 20.0,
    ) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "MailchimpMCPContext":
        return cls(
            os.getenv(
                "RALF_MAILCHIMP_MCP_SOCKET",
                "/run/ralf-mailchimp-mcp/mcp.sock",
            ),
            float(
                os.getenv(
                    "RALF_MAILCHIMP_MCP_TIMEOUT",
                    "20",
                )
            ),
        )

    def __enter__(self) -> MailchimpGateway:
        self.session = MCPClientSession(
            UnixMCPTransport(self.socket_path),
            timeout=self.timeout,
        )
        self.session.__enter__()

        gateway = MailchimpGateway(self.session)
        gateway.discover()
        return gateway

    def __exit__(
        self,
        exc_type,
        exc,
        tb,
    ) -> None:
        if self.session is not None:
            self.session.__exit__(
                exc_type,
                exc,
                tb,
            )


__all__ = [
    "ALL_TOOLS",
    "READ_TOOLS",
    "MailchimpGateway",
    "MailchimpGatewayError",
    "MailchimpMCPContext",
]
