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


def test_live_catalog_autodiscovers_new_provider_socket(tmp_path):
    import socket

    provider_dir = tmp_path / "ralf-new-service-mcp"
    provider_dir.mkdir()
    socket_path = provider_dir / "mcp.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "providers": [],
        "provider_globs": [str(tmp_path / "ralf-*-mcp" / "mcp.sock")],
    }))
    tool = MCPTool("new_read_tool", "Read new service state.", {"type": "object"})
    seen = []

    def factory(path):
        seen.append(path)
        return FakeSession((tool,))

    try:
        tools, health = LiveMCPCatalog(config, session_factory=factory).snapshot()
    finally:
        listener.close()

    assert tools[0].provider == "new_service"
    assert tools[0].name == "new_read_tool"
    assert health[0].status == "available"
    assert seen == [str(socket_path)]


def test_live_catalog_caches_tools_list_until_forced(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "providers": [{"id": "cached", "socket": "/tmp/cached.sock"}],
    }))
    tool = MCPTool("ping", "Health check", {"type": "object"})
    calls = []

    def factory(path):
        calls.append(path)
        return FakeSession((tool,))

    catalog = LiveMCPCatalog(config, session_factory=factory)
    first = catalog.snapshot()
    second = catalog.snapshot()
    assert first == second
    assert calls == ["/tmp/cached.sock"]

    catalog.snapshot(force=True)
    assert calls == ["/tmp/cached.sock", "/tmp/cached.sock"]


def test_live_catalog_expands_configured_term_aliases(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({
        "schema_version": 1,
        "providers": [{"id": "social", "socket": "/tmp/social.sock"}],
        "term_aliases": {"pubblica": ["publish"], "grafica": ["design"]},
    }))
    tools = (
        MCPTool("meta_publish_instagram", "Publish an Instagram post.", {"type": "object"}),
        MCPTool("meta_accounts", "List social accounts.", {"type": "object"}),
        MCPTool("create_design", "Create a design.", {"type": "object"}),
    )
    catalog = LiveMCPCatalog(config, session_factory=lambda _path: FakeSession(tools))

    social = catalog.discover("pubblica su instagram", limit=3)
    design = catalog.discover("crea una grafica", limit=3)

    assert social[0].name == "meta_publish_instagram"
    assert design[0].name == "create_design"
