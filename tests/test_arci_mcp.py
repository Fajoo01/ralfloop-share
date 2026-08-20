from __future__ import annotations

import json

import pytest

from ralfloop_agent.unified_assistant.arci_portal import ArciOrganizationProfile
from scripts.ralf_arci_mcp_server import ArciMCPServer, TOOLS, _response
from src.arci import ArciGateway, READ_TOOL
from src.mcp_client import MCPClient
from src.mcp_transport import MCPProtocolError, MCPTool


def profile() -> ArciOrganizationProfile:
    return ArciOrganizationProfile(
        status="FOUND",
        session_authenticated=True,
        organization_name="TIREMM INNANZ APS",
        member_count=310,
        as_of="2026-08-20",
        governance_total=8,
        governance_dated=8,
        governance_under_15=0,
        governance_age_15_30=0,
        governance_over_30=8,
        provenance=(
            "arci_portal:organization",
            "arci_portal:governance_age_buckets:2026-08-20",
        ),
        read_operations=("arci.organization_profile.read",),
    )


class FakeProvider:
    def read_organization_profile(self):
        return profile()


class FakeSession:
    def __init__(self, *, payload=None, tools=None):
        self.payload = payload
        self.tools = tools
        self.calls = []

    def list_tools(self):
        if self.tools is not None:
            return tuple(self.tools)
        return tuple(
            MCPTool(name, "semantic", schema)
            for name, schema in TOOLS.items()
        )

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        payload = self.payload or {
            "ok": True,
            "operation": "read_organization_profile",
            **profile().model_dump(mode="json"),
            "writes": 0,
            "sends": 0,
        }
        return {"structuredContent": payload}


def test_arci_server_exposes_one_semantic_strict_read_tool():
    server = ArciMCPServer(FakeProvider())

    listed = server.list_tools()
    result = server.call(READ_TOOL, {})

    assert [tool["name"] for tool in listed] == [READ_TOOL]
    assert listed[0]["inputSchema"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    serialized = json.dumps(listed).casefold()
    for forbidden in (
        "selector",
        "xpath",
        "javascript",
        "cdp_method",
        "coordinate",
        '"url"',
    ):
        assert forbidden not in serialized
    payload = result["structuredContent"]
    assert payload["side_effects"] == 0
    assert payload["writes"] == 0
    assert payload["sends"] == 0
    assert payload["governance_age_15_30"] == 0
    assert payload["governance_over_30"] == 8


def test_arci_server_denies_arguments_unknown_tools_and_jsonrpc_mutation():
    server = ArciMCPServer(FakeProvider())

    unsafe = server.call(READ_TOOL, {"selector": "#result_list"})
    unknown = server.call("arci_click", {})
    rpc = _response({
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": READ_TOOL,
            "arguments": {"url": "https://portale.arci.it"},
        },
    }, server)
    non_object = _response({
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": READ_TOOL, "arguments": []},
    }, server)

    assert unsafe["structuredContent"]["status"] == "POLICY_DENIED"
    assert unknown["structuredContent"]["status"] == "POLICY_DENIED"
    assert rpc["result"]["structuredContent"]["status"] == "POLICY_DENIED"
    assert rpc["result"]["structuredContent"]["side_effects"] == 0
    assert non_object["result"]["structuredContent"]["status"] == "POLICY_DENIED"


def test_arci_gateway_validates_discovery_and_pii_minimized_result():
    session = FakeSession()
    gateway = ArciGateway(session)

    assert gateway.discover() == (READ_TOOL,)
    result = gateway.read_organization_profile()

    assert session.calls == [(READ_TOOL, {})]
    assert result["organization_name"] == "TIREMM INNANZ APS"
    assert result["governance_under_15"] == 0
    assert result["governance_age_15_30"] == 0
    assert result["governance_over_30"] == 8
    assert result["side_effects"] == 0


def test_arci_gateway_rejects_generic_schema_and_pii_output():
    generic = MCPTool(
        "arci_click",
        "unsafe",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    session = FakeSession(tools=(*FakeSession().list_tools(), generic))
    with pytest.raises(MCPProtocolError, match="generic_browser_tool"):
        ArciGateway(session).discover()

    mutation = MCPTool(
        "arci_delete_record",
        "unsafe",
        {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    )
    with pytest.raises(MCPProtocolError, match="unexpected_tool"):
        ArciGateway(FakeSession(
            tools=(*FakeSession().list_tools(), mutation),
        )).discover()

    unsafe_schema = MCPTool(
        READ_TOOL,
        "unsafe",
        {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    with pytest.raises(MCPProtocolError, match="schema_not_strict"):
        ArciGateway(FakeSession(tools=(unsafe_schema,))).discover()

    payload = {
        "ok": True,
        "operation": "read_organization_profile",
        **profile().model_dump(mode="json"),
        "member_email": "private@example.invalid",
        "writes": 0,
        "sends": 0,
    }
    pii_gateway = ArciGateway(FakeSession(payload=payload))
    pii_gateway.discover()
    with pytest.raises(MCPProtocolError, match="unapproved_fields"):
        pii_gateway.read_organization_profile()


class FakeGateway:
    def __init__(self):
        self.calls = 0

    def read_organization_profile(self):
        self.calls += 1
        return {
            "ok": True,
            "operation": "read_organization_profile",
            **profile().model_dump(mode="json"),
            "writes": 0,
            "sends": 0,
        }


def test_mcp_client_routes_only_exact_arci_origin_and_path():
    gateway = FakeGateway()
    client = MCPClient(arci_gateway=gateway)

    result = json.loads(client.browser_inspect(
        "https://portale.arci.it/admin/office/circolosoci/"
    ))

    assert result["governance_over_30"] == 8
    assert gateway.calls == 1
    for unsafe in (
        "http://portale.arci.it/admin/office/circolosoci/",
        "https://portale.arci.it.evil.invalid/admin/office/circolosoci/",
        "https://portale.arci.it:444/admin/office/circolosoci/",
        "https://portale.arci.it/admin/office/circolosoci/add/",
        "https://portale.arci.it/admin/office/circolosoci/?next=1",
        "https://portale.arci.it/admin/office/circolosoci/#private",
    ):
        assert client.browser_inspect(unsafe).startswith("Browser inspect mock:")
    assert gateway.calls == 1
