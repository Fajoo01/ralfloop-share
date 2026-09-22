from __future__ import annotations

import json
import os
from pathlib import Path
from collections import deque

import pytest

from src.google_workspace import GoogleWorkspaceGateway
from src.mcp_transport import MCPClientSession, MCPError, MCPProcessDied, MCPProtocolError, MCPTimeout, UnixMCPTransport


class FakeTransport:
    def __init__(self, responses=(), error=None):
        self.responses = deque(responses)
        self.error = error
        self.sent = []
        self.closed = False

    def send(self, message):
        self.sent.append(message)

    def receive(self, timeout):
        if self.error:
            raise self.error
        return self.responses.popleft()

    def close(self):
        self.closed = True


def initialized(*, call_result=None):
    return [
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "manage_email", "inputSchema": {"type": "object"}}]}},
        {"jsonrpc": "2.0", "id": 3, "result": call_result or {"structuredContent": {"messages": []}}},
    ]


def test_initialize_discover_call_structured_and_close():
    transport = FakeTransport(initialized(call_result={"structuredContent": {"ok": True}, "content": []}))
    with MCPClientSession(transport) as session:
        assert [tool.name for tool in session.list_tools()] == ["manage_email"]
        assert session.call_tool("manage_email", {"operation": "search", "email": "test@example.org"})["structuredContent"] == {"ok": True}
    assert transport.closed
    assert transport.sent[1] == {"jsonrpc": "2.0", "method": "notifications/initialized"}


@pytest.mark.parametrize("error", [MCPTimeout("timeout"), MCPProcessDied("dead")])
def test_timeout_and_process_death_fail_closed(error):
    with pytest.raises(type(error)):
        MCPClientSession(FakeTransport(error=error)).initialize()


def test_malformed_response_and_undiscovered_tool_fail_closed():
    session = MCPClientSession(FakeTransport([{"jsonrpc": "2.0", "id": 1, "result": []}]))
    with pytest.raises(MCPProtocolError, match="result_not_object"):
        session.initialize()

    transport = FakeTransport(initialized())
    session = MCPClientSession(transport)
    session.initialize()
    session.list_tools()
    with pytest.raises(MCPProtocolError, match="undiscovered"):
        session.call_tool("shell", {"command": "id"})


def test_model_cannot_supply_process_or_shell_configuration():
    transport = FakeTransport(initialized())
    session = MCPClientSession(transport)
    session.initialize()
    session.list_tools()
    session.call_tool("manage_email", {"operation": "search", "email": "test@example.org", "query": "x"})
    assert transport.sent[-1]["method"] == "tools/call"
    assert transport.sent[-1]["params"]["name"] == "manage_email"


def test_structured_ok_false_is_a_real_tool_error():
    transport = FakeTransport(initialized(call_result={
        "structuredContent": {"ok": False, "error": "provider_denied"},
        "content": [],
    }))
    session = MCPClientSession(transport)
    session.initialize()
    session.list_tools()

    with pytest.raises(MCPError, match="provider_denied"):
        session.call_tool("manage_email", {"operation": "search", "email": "x@example.invalid"})


@pytest.mark.integration
def test_real_broker_initialize_and_tools_list():
    socket_path = os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
    if not Path(socket_path).exists():
        pytest.skip("real MCP broker socket is not installed")
    with MCPClientSession(UnixMCPTransport(socket_path), timeout=20) as session:
        tools = {tool.name: tool for tool in session.list_tools()}
        assert "manage_email" in tools
        schema = tools["manage_email"].input_schema
        assert set(schema["required"]) >= {"operation", "email"}
        assert set(schema["properties"]["operation"]["enum"]) >= {"search", "read", "getThread", "getAttachment", "reply"}


@pytest.mark.integration
def test_real_ralf_read_only_magnolia_search_canary():
    socket_path = os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
    account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "")
    if not Path(socket_path).exists() or not account:
        pytest.skip("real MCP broker/account not configured")
    with MCPClientSession(UnixMCPTransport(socket_path), timeout=30) as session:
        gateway = GoogleWorkspaceGateway(session, account=account)
        gateway.discover()
        result = gateway.invoke("search", query="ARCI Magnolia", maxResults=10)
        assert isinstance(result, dict)


def test_stream_request_yields_matching_notifications_then_result():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "other/event", "params": {"requestId": 2, "event": {"type": "delta", "text": "ignore"}}},
        {"jsonrpc": "2.0", "method": "teacher/stream/event", "params": {"requestId": 2, "event": {"type": "delta", "text": "Ciao"}}},
        {"jsonrpc": "2.0", "id": 2, "result": {"structuredContent": {"ok": True, "response": "Ciao"}, "content": []}},
    ])
    session = MCPClientSession(transport)
    session.initialize()
    events = list(session.stream_request(
        "teacher/stream",
        {"name": "teacher.explain", "arguments": {}},
        notification_method="teacher/stream/event",
    ))
    assert events[0] == {"type": "notification", "event": {"type": "delta", "text": "Ciao"}}
    assert events[1]["type"] == "result"
    assert events[1]["result"]["structuredContent"]["ok"] is True


def test_server_ping_request_is_answered_while_waiting_for_result():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 0, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}},
    ])
    session = MCPClientSession(transport)
    session.initialize()
    assert session.list_tools() == ()
    assert {"jsonrpc": "2.0", "id": 0, "result": {}} in transport.sent


def test_unknown_server_request_gets_method_not_found():
    transport = FakeTransport([
        {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}, "serverInfo": {"name": "test", "version": "1"}}},
        {"jsonrpc": "2.0", "id": 99, "method": "roots/list", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {"tools": []}},
    ])
    session = MCPClientSession(transport)
    session.initialize()
    assert session.list_tools() == ()
    assert any(msg.get("id") == 99 and msg.get("error", {}).get("code") == -32601 for msg in transport.sent)