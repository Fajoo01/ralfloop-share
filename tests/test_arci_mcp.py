from __future__ import annotations

import json

import pytest

from ralfloop_agent.unified_assistant.arci_portal import ArciOrganizationProfile
from scripts.ralf_arci_mcp_server import ArciMCPServer, TOOLS, _response
from src.arci import ArciGateway, READ_TOOL, READ_TOOLS, SEMANTIC_TOOLS
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
        legacy = tuple(
            MCPTool(name, "semantic", schema)
            for name, schema in TOOLS.items()
        )
        present = {tool.name for tool in legacy}
        return legacy + tuple(
            MCPTool(name, "semantic", {
                "type": "object", "properties": {}, "required": [],
                "additionalProperties": False,
            }) for name in sorted(READ_TOOLS - present)
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

    assert gateway.discover() == tuple(sorted(READ_TOOLS))
    result = gateway.read_organization_profile()

    assert session.calls == [(READ_TOOL, {})]
    assert result["organization_name"] == "TIREMM INNANZ APS"
    assert result["governance_under_15"] == 0
    assert result["governance_age_15_30"] == 0
    assert result["governance_over_30"] == 8
    assert result["side_effects"] == 0


def partial_rest_payload(**overrides):
    payload = {
        "ok": True,
        "operation": "read_organization_profile",
        "status": "PARTIAL",
        "session_authenticated": True,
        "organization_name": "TIREMM INNANZ APS",
        "member_count": None,
        "as_of": None,
        "governance_total": None,
        "governance_dated": None,
        "governance_under_15": None,
        "governance_age_15_30": None,
        "governance_over_30": None,
        "provenance": ["arci_rest:user.cards.club"],
        "read_operations": ["arci.rest.user.read"],
        "write_operations": 0,
        "side_effects": 0,
        "content_role": "data",
        "writes": 0,
        "sends": 0,
    }
    payload.update(overrides)
    return payload


def gateway_result(payload):
    gateway = ArciGateway(FakeSession(payload=payload))
    gateway.discover()
    return gateway.read_organization_profile()


def test_arci_gateway_accepts_partial_rest_profile_without_governance():
    result = gateway_result(partial_rest_payload())

    assert result["status"] == "PARTIAL"
    assert result["organization_name"] == "TIREMM INNANZ APS"
    assert result["member_count"] is None
    assert result["governance_total"] is None
    assert result["governance_dated"] is None
    assert result["governance_under_15"] is None
    assert result["governance_age_15_30"] is None
    assert result["governance_over_30"] is None
    assert result["side_effects"] == 0
    assert result["writes"] == 0
    assert result["sends"] == 0


def test_arci_gateway_keeps_complete_legacy_governance_validation():
    result = gateway_result(FakeSession().call_tool(READ_TOOL, {})["structuredContent"])

    assert result["status"] == "FOUND"
    assert result["governance_total"] == 8
    assert result["governance_dated"] == 8


def test_arci_gateway_rejects_complete_inconsistent_governance():
    payload = FakeSession().call_tool(READ_TOOL, {})["structuredContent"]
    payload["governance_over_30"] = 7

    with pytest.raises(MCPProtocolError, match="arci_result_invalid"):
        gateway_result(payload)


@pytest.mark.parametrize("overrides", [
    {"governance_total": 8},
    {
        "governance_total": 8,
        "governance_dated": 8,
        "governance_under_15": 0,
    },
])
def test_arci_gateway_rejects_partially_available_governance(overrides):
    with pytest.raises(MCPProtocolError, match="arci_result_invalid"):
        gateway_result(partial_rest_payload(**overrides))


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
    other_tools = tuple(tool for tool in FakeSession().list_tools() if tool.name != READ_TOOL)
    with pytest.raises(MCPProtocolError, match="schema_not_strict"):
        ArciGateway(FakeSession(tools=(unsafe_schema, *other_tools))).discover()

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


@pytest.mark.parametrize("tool,method,payload", [
    ("arci_read_club", "read_club", {
        "name": "TIREMM INNANZ APS", "code": "D061661", "type": "club",
        "committee_code": "D06", "regional_code": "D00", "validity_year": 2026,
        "manually_disabled": False, "digitization_enabled": True,
    }),
    ("arci_read_current_cards", "read_current_cards", {"count": 1, "cards": [{
        "status": 20, "validity": "2026", "expired": False, "enabled_at": None,
        "disabled_at": None, "preregistration": False, "consumer_movement_status": None,
    }]}),
    ("arci_read_committee", "read_committee", {"name": "Committee", "code": "D06", "active": True}),
    ("arci_read_regional", "read_regional", {"name": "Regional", "code": "D00", "consumer_movement_active": False}),
    ("arci_read_dashboard_alerts", "read_dashboard_alerts", {"count": 1, "alerts": [{
        "title": "Notice", "description": "Text", "enabled": True, "updated_at": "2026-08-27",
    }]}),
])
def test_semantic_gateway_reads_are_strict_and_zero_effect(tool, method, payload):
    operation = SEMANTIC_TOOLS[tool]
    result_payload = {
        "ok": True, "operation": operation, "status": "FOUND", **payload,
        "provenance": [f"arci_rest:{operation}"], "read_operations": [f"arci.rest.{operation}"],
        "write_operations": 0, "side_effects": 0, "content_role": "data", "writes": 0, "sends": 0,
    }
    session = FakeSession(payload=result_payload)
    gateway = ArciGateway(session)
    gateway.discover()
    result = getattr(gateway, method)()
    assert session.calls == [(tool, {})]
    assert result["operation"] == operation
    assert (result["side_effects"], result["writes"], result["sends"]) == (0, 0, 0)


def test_semantic_gateway_rejects_pii_and_effects():
    base = {
        "ok": True, "operation": "read_current_cards", "status": "FOUND",
        "count": 1, "cards": [{"status": 20, "email": "private@example.invalid"}],
        "provenance": ["arci_rest:user.cards"], "read_operations": ["arci.rest.user.read"],
        "write_operations": 0, "side_effects": 0, "content_role": "data", "writes": 0, "sends": 0,
    }
    gateway = ArciGateway(FakeSession(payload=base)); gateway.discover()
    with pytest.raises(MCPProtocolError, match="unapproved_fields"):
        gateway.read_current_cards()
    base["cards"] = [{"status": 20}]
    base["side_effects"] = 1
    gateway = ArciGateway(FakeSession(payload=base)); gateway.discover()
    with pytest.raises(MCPProtocolError, match="reported_side_effect"):
        gateway.read_current_cards()


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
