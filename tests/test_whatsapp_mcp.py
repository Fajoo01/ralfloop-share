from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass

import pytest

from scripts.ralf_whatsapp_mcp_server import TOOLS, WhatsAppMCPServer
from src.mcp_transport import MCPProtocolError, MCPTool
from src.whatsapp import READ_TOOLS, WhatsAppGateway, WhatsAppGatewayError


class FakeSession:
    def __init__(self):
        self.calls = []

    def list_tools(self):
        return tuple(MCPTool(name, "semantic", schema) for name, schema in TOOLS.items())

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return {"structuredContent": {
            "ok": True, "operation": name.removeprefix("whatsapp_"),
            "results": [], "side_effects": 0,
        }}


class FakeProvider:
    def list_chats(self):
        return ()


def test_discovery_exposes_only_semantic_strict_tools():
    session = FakeSession()
    gateway = WhatsAppGateway(session)

    names = gateway.discover()

    assert READ_TOOLS <= set(names)
    serialized = str(TOOLS).casefold()
    for forbidden in ("selector", "xpath", "javascript", "cdp_method", "coordinate", "url"):
        assert forbidden not in serialized
    for forbidden_tool in (
        "generic_click", "generic_evaluate", "generic_navigate", "delete",
        "edit", "forward", "reaction", "archive", "call", "settings", "logout",
    ):
        assert forbidden_tool not in names
    assert all(schema["additionalProperties"] is False for schema in TOOLS.values())


def test_read_call_reports_zero_side_effects_and_rejects_unsafe_arguments():
    session = FakeSession()
    gateway = WhatsAppGateway(session)
    gateway.discover()

    result = gateway.invoke_read("whatsapp_list_chats", limit=20)

    assert result["side_effects"] == 0
    with pytest.raises(WhatsAppGatewayError, match="unsafe_argument"):
        gateway.invoke_read("whatsapp_search_chats", query="Marco", selector="#pane-side")
    with pytest.raises(WhatsAppGatewayError, match="read_tool_denied"):
        gateway.invoke_read("whatsapp_send_message", chat_id="wa_chat_" + "a" * 16)


def test_gateway_rejects_generic_browser_tool_at_discovery():
    session = FakeSession()
    original = session.list_tools
    session.list_tools = lambda: (*original(), MCPTool(
        "generic_click", "unsafe", {"type": "object", "properties": {}, "additionalProperties": False}
    ))

    with pytest.raises(MCPProtocolError, match="generic_browser_tool"):
        WhatsAppGateway(session).discover()


def test_server_denies_unknown_mutation_and_read_has_zero_writes():
    server = WhatsAppMCPServer(FakeProvider())

    read = server.call("whatsapp_list_chats", {"limit": 20})
    denied = server.call("whatsapp_delete_chat", {})

    assert read["structuredContent"]["writes"] == 0
    assert read["structuredContent"]["sends"] == 0
    assert denied["structuredContent"]["status"] == "POLICY_DENIED"
    assert denied["structuredContent"]["side_effects"] == 0


def test_whatsapp_systemd_unit_uses_shared_group_broker():
    from pathlib import Path

    unit = Path("deploy/systemd/ralf-whatsapp-mcp-broker.service").read_text()
    assert "Group=ralf-mcp" in unit
    assert "RuntimeDirectoryMode=2770" in unit
    assert "--allow-group ralf-mcp" in unit
    assert "ralf_arci_mcp_broker.py" in unit
    assert "ralf_whatsapp_mcp_server.py" in unit
    assert "/opt/ralfloop" not in unit
    assert "ProtectSystem=strict" in unit
    assert "IPAddressAllow=localhost" in unit
