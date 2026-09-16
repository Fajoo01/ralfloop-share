from __future__ import annotations

import os
from typing import Any, Callable, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

READ_TOOLS = frozenset({
    "bandi_latest",
    "bandi_search_latest",
    "bandi_get_opportunity",
})
REFRESH_TOOL = "bandi_research_now"
ALL_TOOLS = READ_TOOLS | {REFRESH_TOOL}


def _payload(result: Mapping[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if not isinstance(structured, Mapping):
        raise MCPProtocolError("bandi_tool_malformed")
    payload = dict(structured)
    if result.get("isError") or payload.get("ok") is False:
        raise MCPProtocolError(str(payload.get("status") or "bandi_tool_error"))
    return payload


class BandiLiveMCPContext:
    def __init__(
        self,
        socket_path: str | None = None,
        *,
        timeout: float = 8.0,
        session_factory: Callable[[str, float], MCPClientSession] | None = None,
    ) -> None:
        self.socket_path = socket_path or os.getenv(
            "RALF_BANDI_MCP_SOCKET", "/tmp/ralf-bandi-mcp/mcp.sock"
        )
        self.timeout = timeout
        self.session_factory = session_factory or self._session
        self.session: MCPClientSession | None = None

    @staticmethod
    def _session(path: str, timeout: float) -> MCPClientSession:
        return MCPClientSession(
            UnixMCPTransport(path, connect_timeout=0.8),
            timeout=timeout,
            client_name="ralfloop-bandi-live",
        )

    def __enter__(self) -> "BandiLiveMCPContext":
        self.session = self.session_factory(self.socket_path, self.timeout)
        self.session.__enter__()
        found = {tool.name for tool in self.session.list_tools()}
        missing = ALL_TOOLS - found
        if missing:
            self.session.__exit__(None, None, None)
            self.session = None
            raise MCPProtocolError(
                "bandi_required_tools_missing:" + ",".join(sorted(missing))
            )
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            self.session.__exit__(exc_type, exc, tb)
            self.session = None

    def _call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if self.session is None or name not in ALL_TOOLS:
            raise MCPProtocolError("bandi_tool_not_available")
        return _payload(self.session.call_tool(name, dict(arguments)))

    def latest(self, *, limit: int = 12) -> dict[str, Any]:
        return self._call("bandi_latest", {"limit": int(limit)})

    def search(self, query: str, *, limit: int = 10) -> dict[str, Any]:
        return self._call(
            "bandi_search_latest", {"query": str(query)[:500], "limit": int(limit)}
        )

    def get(self, call_key: str) -> dict[str, Any]:
        return self._call("bandi_get_opportunity", {"call_key": str(call_key)})

    def refresh(self, focus: str = "", *, limit: int = 8) -> dict[str, Any]:
        args: dict[str, Any] = {"limit": int(limit)}
        if focus.strip():
            args["focus"] = focus.strip()[:500]
        return self._call(REFRESH_TOOL, args)


__all__ = [
    "ALL_TOOLS", "READ_TOOLS", "REFRESH_TOOL", "BandiLiveMCPContext",
]
