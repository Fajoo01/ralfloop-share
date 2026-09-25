from __future__ import annotations

import os
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPError, MCPProtocolError, UnixMCPTransport

READ_TOOLS = frozenset({
    "github_repo_get",
    "github_issue_list",
    "github_issue_get",
    "github_pr_list",
    "github_pr_get",
})
WRITE_TOOLS = frozenset({"github_issue_create", "github_issue_comment"})


class GitHubGateway:
    """Strict semantic client for the local GitHub MCP broker."""

    def __init__(self, session: MCPClientSession) -> None:
        self.session = session
        self.tools: set[str] = set()

    def discover(self) -> set[str]:
        self.tools = {tool.name for tool in self.session.list_tools()}
        missing = (READ_TOOLS | WRITE_TOOLS) - self.tools
        if missing:
            raise MCPProtocolError("github_mcp_missing_tools:" + ",".join(sorted(missing)))
        return set(self.tools)

    def invoke(self, name: str, **arguments: Any) -> dict[str, Any]:
        if name not in READ_TOOLS | WRITE_TOOLS:
            raise ValueError("github_tool_not_allowed")
        if not self.tools:
            self.discover()
        result = self.session.call_tool(name, arguments)
        payload = result.get("structuredContent") if isinstance(result, Mapping) else None
        if not isinstance(payload, Mapping):
            raise MCPProtocolError("github_mcp_invalid_payload")
        return dict(payload)


def build_session_from_env() -> MCPClientSession:
    socket_path = os.getenv("RALF_GITHUB_MCP_SOCKET", "/run/ralf-github-mcp/mcp.sock")
    timeout = float(os.getenv("RALF_GITHUB_MCP_TIMEOUT", "20"))
    return MCPClientSession(UnixMCPTransport(socket_path), timeout=timeout, client_name="ralf-github")


class GitHubReadContext:
    def __init__(self, socket_path: str | None = None, timeout: float | None = None) -> None:
        self.socket_path = socket_path or os.getenv("RALF_GITHUB_MCP_SOCKET", "/run/ralf-github-mcp/mcp.sock")
        self.timeout = timeout or float(os.getenv("RALF_GITHUB_MCP_TIMEOUT", "20"))
        self.session: MCPClientSession | None = None
        self.gateway: GitHubGateway | None = None

    def __enter__(self) -> GitHubGateway:
        self.session = MCPClientSession(UnixMCPTransport(self.socket_path), timeout=self.timeout, client_name="ralf-github")
        self.session.__enter__()
        self.gateway = GitHubGateway(self.session)
        self.gateway.discover()
        return self.gateway

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            self.session.__exit__(exc_type, exc, tb)
        self.session = None
        self.gateway = None


__all__ = [
    "GitHubGateway",
    "GitHubReadContext",
    "READ_TOOLS",
    "WRITE_TOOLS",
    "build_session_from_env",
    "MCPError",
]
