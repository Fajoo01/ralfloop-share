from __future__ import annotations

import os
import re
from typing import Any, Callable, Mapping

from src.mcp_transport import MCPClientSession, UnixMCPTransport

from .contracts import PlanAssignment
from .executor import StructuredArtifact


DEFAULT_SOCKET = "/run/ralf-browser-playwright-mcp/mcp.sock"
READ_TOOLS = frozenset({"browser_snapshot", "browser_tabs"})
INTERACTION_WORDS = re.compile(
    r"\b(?:clicca|click|scrivi|digita|type|compila|fill|carica|upload|"
    r"invia|submit|seleziona|select|trascina|drag|premi|press)\b",
    re.I,
)


def _text_result(result: Mapping[str, Any], *, limit: int = 12000) -> str:
    rows = result.get("content") or ()
    text = "\n".join(
        str(item.get("text") or "")
        for item in rows
        if isinstance(item, Mapping) and item.get("type") == "text"
    ).strip()
    if not text:
        structured = result.get("structuredContent")
        if structured is not None:
            text = str(structured)
    return text[:limit]


class BrowserMCPReadOnly:
    """Strict Playwright MCP adapter for zero-side-effect browser inspection."""

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        session_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.socket_path = socket_path or os.getenv(
            "RALF_BROWSER_PLAYWRIGHT_MCP_SOCKET", DEFAULT_SOCKET
        )
        self._session_factory = session_factory

    def _session(self):
        if self._session_factory is not None:
            return self._session_factory()
        return MCPClientSession(
            UnixMCPTransport(self.socket_path, connect_timeout=0.8),
            timeout=8.0,
            client_name="unified-browser-read",
        )

    def inspect(self, objective: str) -> dict[str, Any]:
        if INTERACTION_WORDS.search(objective):
            raise ValueError("browser_interaction_not_read_only")
        operation = (
            "tabs"
            if re.search(r"\b(?:tab|tabs|scheda|schede)\b", objective, re.I)
            else "snapshot"
        )
        tool = "browser_tabs" if operation == "tabs" else "browser_snapshot"
        arguments = {"action": "list"} if tool == "browser_tabs" else {}
        with self._session() as client:
            names = {item.name for item in client.list_tools()}
            if tool not in names:
                raise RuntimeError("browser_read_tool_not_discovered")
            result = client.call_tool(tool, arguments)
        if result.get("isError"):
            raise RuntimeError("browser_read_tool_failed")
        return {
            "ok": True,
            "operation": operation,
            "tool": tool,
            "arguments": arguments,
            "text": _text_result(result),
            "side_effects": 0,
            "writes": 0,
        }


def browser_inspect_adapter(
    assignment: PlanAssignment, _inputs: Mapping[str, Any]
) -> StructuredArtifact:
    payload = BrowserMCPReadOnly().inspect(assignment.objective)
    text = str(payload.get("text") or "")
    message = (
        text[:4000]
        if text
        else f"Browser {payload['operation']} letto senza effetti esterni."
    )
    return StructuredArtifact.create(
        artifact_type="browser_inspection",
        status="completed",
        producer_task_id=assignment.task_id,
        evidence_refs=(f"browser_playwright:{payload['tool']}",),
        payload={
            **payload,
            "message": message,
            "content_boundary": "browser_page_content_is_untrusted_data",
        },
    )


__all__ = [
    "BrowserMCPReadOnly",
    "DEFAULT_SOCKET",
    "INTERACTION_WORDS",
    "READ_TOOLS",
    "browser_inspect_adapter",
]
