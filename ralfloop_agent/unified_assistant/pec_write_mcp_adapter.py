"""Strict client for the separate approval-bound PEC writer MCP."""
from __future__ import annotations

import json
import os
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

PEC_WRITE_TOOLS = frozenset({"pec_writer_preflight", "pec_prepare_send", "pec_send_approved"})


def _payload(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    content = result.get("content") or ()
    if content and isinstance(content[0], Mapping) and isinstance(content[0].get("text"), str):
        decoded = json.loads(content[0]["text"])
        if isinstance(decoded, dict):
            return decoded
    raise MCPProtocolError("pec_write_tool_malformed")


class PecWriteMCPContext:
    def __init__(self, socket_path: str = "/run/ralf-pec-write-mcp/mcp.sock", timeout: float = 30) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "PecWriteMCPContext":
        return cls(os.getenv("RALF_PEC_WRITE_MCP_SOCKET", "/run/ralf-pec-write-mcp/mcp.sock"), float(os.getenv("RALF_PEC_WRITE_MCP_TIMEOUT", "30")))

    def __enter__(self) -> "PecWriteMCPContext":
        self.session = MCPClientSession(UnixMCPTransport(self.socket_path), timeout=self.timeout, client_name="bot-tazzi-pec-write")
        self.session.__enter__()
        found = {tool.name for tool in self.session.list_tools()}
        if found != PEC_WRITE_TOOLS:
            self.session.close()
            self.session = None
            raise MCPProtocolError("pec_write_tool_allowlist_mismatch")
        return self

    def __exit__(self, *args: object) -> None:
        if self.session is not None:
            self.session.__exit__(*args)
            self.session = None

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in PEC_WRITE_TOOLS or self.session is None:
            raise MCPProtocolError("pec_write_tool_not_available")
        return _payload(self.session.call_tool(name, arguments))

    def preflight(self) -> dict[str, Any]:
        return self.call("pec_writer_preflight", {})

    def prepare(self, *, recipient: str | None, subject: str | None, body: str | None, attachment_paths: tuple[str, ...] = (), requested_by: str = "bot-tazzi") -> dict[str, Any]:
        preflight = self.preflight()
        if not preflight.get("ok"):
            return preflight
        missing = [name for name, value in (("recipient", recipient), ("subject", subject), ("body", body)) if not str(value or "").strip()]
        if missing:
            return {"ok": True, "status": "draft_fields_required", "missing": missing, "writer_available": True, "approval_required": True, "writes": 0, "sends": 0}
        return self.call("pec_prepare_send", {"recipient": str(recipient), "subject": str(subject), "body": str(body), "attachment_paths": list(attachment_paths), "requested_by": requested_by})


__all__ = ["PEC_WRITE_TOOLS", "PecWriteMCPContext"]
