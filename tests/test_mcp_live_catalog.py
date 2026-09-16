from __future__ import annotations

import json

from ralfloop_agent.unified_assistant.mcp_live_catalog import LiveMCPCatalog
from src.mcp_transport import MCPTool


class FakeSession:
    def __init__(self, tools=(), *, fail=False, trace=None):
        self.tools = tuple(tools)
        self.fail = fail
        self.trace = trace if trace is not None else []

    def __enter__(self):
        self.trace.append("initialize")
        if self.fail:
            raise RuntimeError("offline")
        return self

    def __exit__(self, *_):
        self.trace.append("close")

    def list_tools(self):
        self.trace.append("tools/list")
        return self.tools

    def call_tool(self, *_args, **_kwargs):
        raise AssertionError("live catalog must never call tools/call")


def test_live_mcp_catalog_discovers_tools_without_executing_them(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "providers": [
            {"id": "google_workspace", "socket": "/tmp/google.sock"},
            {"id": "offline", "socket": "/tmp/offline.sock"},
        ],
    }))
    trace = []
    tools = (
        MCPTool("manage_calendar", "Read and manage calendar operations.", {"type": "object"}),
        MCPTool("manage_email", "Read and manage email operations.", {"type": "object"}),
    )

    def factory(socket_path):
        return FakeSession(tools if "google" in socket_path else (), fail="offline" in socket_path, trace=trace)

    catalog = LiveMCPCatalog(config, session_factory=factory)
    rows = catalog.discover("calendar", limit=5)
    assert rows[0].provider == "google_workspace"
    assert rows[0].name == "manage_calendar"
    assert "tools/list" in trace


def test_live_mcp_catalog_reports_provider_health(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "providers": [
            {"id": "ok", "socket": "/tmp/ok.sock"},
            {"id": "bad", "socket": "/tmp/bad.sock"},
        ],
    }))
    tool = MCPTool("ping", "Health check", {"type": "object"})

    def factory(socket_path):
        return FakeSession((tool,), fail="bad" in socket_path)

    catalog = LiveMCPCatalog(config, session_factory=factory)
    tools, health = catalog.snapshot()
    assert len(tools) == 1
    states = {row.provider: row for row in health}
    assert states["ok"].status == "available"
    assert states["ok"].tool_count == 1
    assert states["bad"].status == "unavailable"
    assert "RuntimeError" in (states["bad"].error or "")
