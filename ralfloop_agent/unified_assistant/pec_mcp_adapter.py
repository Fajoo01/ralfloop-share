"""Strict read-only Unix-MCP adapter for the standalone Tiremm PEC vertical."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

PEC_TOOLS = frozenset({
    "pec_discover_messages",
    "pec_get_message",
    "pec_list_attachments",
    "pec_get_attachment",
    "pec_search_messages",
})


def _payload(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        structured = result.get("structuredContent")
        code = structured.get("status") if isinstance(structured, Mapping) else "pec_tool_error"
        raise MCPProtocolError(str(code or "pec_tool_error"))
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    content = result.get("content") or ()
    if content and isinstance(content[0], Mapping) and isinstance(content[0].get("text"), str):
        decoded = json.loads(content[0]["text"])
        if isinstance(decoded, dict):
            return decoded
    raise MCPProtocolError("pec_tool_malformed")


class PecMCPContext:
    """Least-privilege client for the five read-only PEC MCP operations."""

    def __init__(self, socket_path: str = "/run/ralf-pec-mcp/mcp.sock", timeout: float = 90):
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "PecMCPContext":
        return cls(os.getenv("RALF_PEC_MCP_SOCKET", "/run/ralf-pec-mcp/mcp.sock"))

    def __enter__(self) -> "PecMCPContext":
        self.session = MCPClientSession(
            UnixMCPTransport(self.socket_path), timeout=self.timeout, client_name="bot-tazzi-pec"
        )
        self.session.__enter__()
        found = {tool.name for tool in self.session.list_tools()}
        if found != PEC_TOOLS:
            self.session.close()
            self.session = None
            raise MCPProtocolError("pec_tool_allowlist_mismatch")
        return self

    def __exit__(self, *args: object) -> None:
        if self.session is not None:
            self.session.__exit__(*args)
            self.session = None

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in PEC_TOOLS or self.session is None:
            raise MCPProtocolError("pec_tool_not_available")
        return _payload(self.session.call_tool(name, arguments))

    def request(self, objective: str) -> dict[str, Any]:
        """Retrieve the smallest useful PEC evidence set for a natural-language objective."""
        folded = objective.casefold()
        address = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b", objective, re.I)
        query = None
        if address:
            query = address.group(0)
        else:
            for marker in ("difensore", "tari", "documentazione", "integrazione", "protocollo"):
                if marker in folded:
                    query = marker
                    break

        if query:
            payload = self.call("pec_search_messages", {"query": query, "limit": 100})
            operation = "search"
        else:
            payload = self.call("pec_discover_messages", {"limit": 30})
            operation = "recent"

        messages = list(payload.get("messages") or ())
        payload["operation"] = operation
        payload["query"] = query
        payload["message"] = _message(messages, query)
        payload["evidence_refs"] = [
            str(item.get("source", {}).get("locator") or item.get("native_id") or "")
            for item in messages[:12]
            if isinstance(item, Mapping)
        ]
        payload["writes"] = 0
        payload["sends"] = 0
        return payload


def _message(messages: list[Any], query: str | None) -> str:
    rows = [item for item in messages if isinstance(item, Mapping)]
    if not rows:
        return f"Nessuna PEC trovata per {query}." if query else "Nessuna PEC recente trovata."
    lines = [f"PEC trovate: {len(rows)}."]
    for item in rows[:5]:
        sender = str(item.get("sender") or "mittente sconosciuto")
        subject = str(item.get("subject") or "senza oggetto")
        received = str(item.get("received_at") or "")
        lines.append(f"- {received} | {sender} | {subject}")
    return "\n".join(lines)


__all__ = ["PEC_TOOLS", "PecMCPContext"]
