from __future__ import annotations

import json
import os
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

from .tuya_mcp import TUYA_TOOLS


class TuyaMCPProviderError(RuntimeError):
    pass


def _payload(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        structured = result.get("structuredContent")
        code = structured.get("status") if isinstance(structured, Mapping) else "tuya_tool_error"
        raise TuyaMCPProviderError(str(code or "tuya_tool_error"))
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    content = result.get("content") or ()
    if content and isinstance(content[0], Mapping) and isinstance(content[0].get("text"), str):
        decoded = json.loads(content[0]["text"])
        if isinstance(decoded, dict):
            return decoded
    raise TuyaMCPProviderError("tuya_tool_malformed")
class TuyaMCPContext:
    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "TuyaMCPContext":
        return cls(os.getenv("RALF_TUYA_MCP_SOCKET", "/run/ralf-tuya-mcp/mcp.sock"))

    def __enter__(self) -> "TuyaMCPContext":
        try:
            self.session = MCPClientSession(
                UnixMCPTransport(self.socket_path),
                timeout=self.timeout,
                client_name="bot-tazzi-tuya",
            )
            self.session.__enter__()
            found = {tool.name for tool in self.session.list_tools()}
            if found != TUYA_TOOLS:
                raise TuyaMCPProviderError("tuya_tool_allowlist_mismatch")
            return self
        except (OSError, MCPProtocolError) as exc:
            self.close()
            raise TuyaMCPProviderError("tuya_mcp_unavailable") from exc

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None
    def __exit__(self, *args: object) -> None:
        self.close()

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self.session is None or name not in TUYA_TOOLS:
            raise TuyaMCPProviderError("tuya_tool_not_available")
        try:
            return _payload(self.session.call_tool(name, arguments))
        except MCPProtocolError as exc:
            raise TuyaMCPProviderError("tuya_mcp_protocol_error") from exc


class TuyaMCPHomeBackend:
    """HomeWorkflow backend that delegates all live Tuya I/O to the dedicated MCP."""

    def __init__(self, socket_path: str, timeout: float = 30.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    @classmethod
    def from_environment(cls) -> "TuyaMCPHomeBackend":
        return cls(os.getenv("RALF_TUYA_MCP_SOCKET", "/run/ralf-tuya-mcp/mcp.sock"))

    def _call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        with TuyaMCPContext(self.socket_path, self.timeout) as gateway:
            return gateway.call(name, arguments)

    def health(self) -> dict[str, Any]:
        return self._call("tuya_health", {})
    def list_entities(self) -> tuple[dict[str, Any], ...]:
        payload = self._call("tuya_list_entities", {"limit": 300})
        return tuple(
            item.get("state") or {}
            for item in payload.get("items") or ()
            if isinstance(item, Mapping)
        )

    def read_state(self, entity_id: str) -> dict[str, Any]:
        payload = self._call("tuya_get_state", {"entity_id": entity_id})
        state = payload.get("state")
        if not isinstance(state, Mapping):
            raise TuyaMCPProviderError("tuya_state_invalid")
        return dict(state)

    def call_service(self, service: str, entity_id: str, data: dict[str, Any]) -> Any:
        return self._call(
            "tuya_call_service",
            {"entity_id": entity_id, "service": service, "data": dict(data)},
        )


__all__ = [
    "TuyaMCPContext",
    "TuyaMCPHomeBackend",
    "TuyaMCPProviderError",
]
