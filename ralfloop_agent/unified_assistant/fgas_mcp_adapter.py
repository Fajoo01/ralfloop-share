"""Strict Bot-tazzi adapter for the local F-Gas installation MCP.

This adapter may read evidence and create local DOCX/PDF artifacts.
It never sends email/WhatsApp, publishes, uploads, or shares externally.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

FGAS_TOOLS = frozenset({
    "fgas_drive_status",
    "fgas_extract_from_text",
    "fgas_validate_installation",
    "fgas_render_installation",
    "fgas_prepare_from_whatsapp",
})
_SEND_RE = re.compile(r"\b(?:invia|manda|spedisci|inoltra)\b", re.I)


def _payload(result: Mapping[str, Any]) -> dict[str, Any]:
    if result.get("isError"):
        structured = result.get("structuredContent")
        code = structured.get("status") if isinstance(structured, Mapping) else "fgas_tool_error"
        raise MCPProtocolError(str(code or "fgas_tool_error"))
    structured = result.get("structuredContent")
    if isinstance(structured, Mapping):
        return dict(structured)
    content = result.get("content") or ()
    if content and isinstance(content[0], Mapping) and isinstance(content[0].get("text"), str):
        decoded = json.loads(content[0]["text"])
        if isinstance(decoded, dict):
            return decoded
    raise MCPProtocolError("fgas_tool_malformed")


class FGasMCPContext:
    def __init__(self, socket_path: str = "/run/ralf-fgas-mcp/mcp.sock", timeout: float = 240):
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "FGasMCPContext":
        return cls(os.getenv("RALF_FGAS_MCP_SOCKET", "/run/ralf-fgas-mcp/mcp.sock"))

    def __enter__(self) -> "FGasMCPContext":
        self.session = MCPClientSession(
            UnixMCPTransport(self.socket_path), timeout=self.timeout, client_name="bot-tazzi-fgas"
        )
        self.session.__enter__()
        found = {tool.name for tool in self.session.list_tools()}
        if found != FGAS_TOOLS:
            self.session.close()
            self.session = None
            raise MCPProtocolError("fgas_tool_allowlist_mismatch")
        return self

    def __exit__(self, *args: object) -> None:
        if self.session is not None:
            self.session.__exit__(*args)
            self.session = None

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in FGAS_TOOLS or self.session is None:
            raise MCPProtocolError("fgas_tool_not_available")
        return _payload(self.session.call_tool(name, dict(arguments)))

    def request(self, objective: str) -> dict[str, Any]:
        lowered = objective.casefold()
        if re.search(r"\b(?:stato|template|modello\s+vuoto)\b", lowered):
            raw = self.call("fgas_drive_status", {})
        else:
            raw = self.call("fgas_prepare_from_whatsapp", {})

        # The F-Gas MCP is intentionally artifact-only. Any external action
        # must be staged separately by the application's approval workflow.
        if int(raw.get("external_sends") or 0) != 0 or int(raw.get("external_writes") or 0) != 0:
            raise MCPProtocolError("fgas_external_side_effect_reported")

        render = raw.get("render") if isinstance(raw.get("render"), Mapping) else {}
        artifacts = render.get("artifacts") if isinstance(render, Mapping) else {}
        if not isinstance(artifacts, Mapping):
            artifacts = {}
        complete = bool(raw.get("complete"))
        status = str(raw.get("status") or ("COMPLETE" if complete else "INCOMPLETE"))
        send_requested = bool(_SEND_RE.search(objective))
        message = (
            "Modulo F-Gas preparato e verificato localmente."
            if complete else
            "Modulo F-Gas preparato ma incompleto: servono i dati mancanti indicati."
        )
        if send_requested:
            message += " L'invio esterno non è stato eseguito: deve passare dall'approvazione dell'applicazione."
        return {
            **raw,
            "status": status,
            "send_requested": send_requested,
            "message": message,
            "artifact_paths": [
                str(artifacts[key]) for key in ("docx", "pdf")
                if artifacts.get(key)
            ],
            "external_sends": 0,
            "external_writes": 0,
            "approval_required_for_external_send": True,
        }


__all__ = ["FGAS_TOOLS", "FGasMCPContext"]
