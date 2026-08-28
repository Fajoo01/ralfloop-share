from types import SimpleNamespace

import pytest

from src.mailchimp import (
    ALL_TOOLS,
    MailchimpGateway,
    MailchimpGatewayError,
    READ_TOOLS,
)
from src.mcp_transport import MCPProtocolError


def schema(properties=None):
    return {
        "type": "object",
        "properties": properties or {},
        "required": [],
        "additionalProperties": False,
    }


def valid_tools():
    paging = {
        "count": {"type": "integer", "minimum": 1, "maximum": 1000},
        "offset": {"type": "integer", "minimum": 0, "maximum": 1000000},
    }
    resource = {"type": "string", "pattern": r"^[A-Za-z0-9_-]{1,128}$"}
    tools = [
        SimpleNamespace(
            name="mailchimp_ping",
            input_schema=schema(),
        ),
        SimpleNamespace(
            name="mailchimp_list_audiences",
            input_schema=schema(paging),
        ),
        SimpleNamespace(
            name="mailchimp_list_campaigns",
            input_schema=schema(paging),
        ),
        SimpleNamespace(name="mailchimp_get_campaign_content", input_schema={
            **schema({"campaign_id": resource}), "required": ["campaign_id"],
        }),
        SimpleNamespace(name="mailchimp_list_members", input_schema={
            **schema({"list_id": resource, **paging, "status": {"type": "string", "enum": ["subscribed"]}}),
            "required": ["list_id"],
        }),
        SimpleNamespace(name="mailchimp_list_segments", input_schema={
            **schema({"list_id": resource, **paging}), "required": ["list_id"],
        }),
        SimpleNamespace(name="mailchimp_list_tags", input_schema={
            **schema({"list_id": resource, "name": {"type": "string", "minLength": 1, "maxLength": 255}}),
            "required": ["list_id"],
        }),
        SimpleNamespace(name="mailchimp_list_member_tags", input_schema={
            **schema({"list_id": resource, "subscriber_hash": {"type": "string", "pattern": r"^[a-fA-F0-9]{32}$"}}),
            "required": ["list_id", "subscriber_hash"],
        }),
    ]
    protected = schema({
        "approval_request_id": {"type": "string", "minLength": 1},
        "execution_id": {"type": "string", "minLength": 1},
    })
    tools.extend([
        SimpleNamespace(name="mailchimp_create_approved_campaign", input_schema=protected),
        SimpleNamespace(name="mailchimp_send_approved_campaign", input_schema=protected),
    ])
    return tools


class FakeSession:
    def __init__(self, tools=None, result=None):
        self.tools = valid_tools() if tools is None else tools
        self.result = result or {
            "ok": True,
            "side_effects": 0,
            "writes": 0,
            "sends": 0,
        }
        self.calls = []

    def list_tools(self):
        return self.tools

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return self.result


def test_discover_accepts_exact_read_and_protected_contract():
    gateway = MailchimpGateway(FakeSession())

    discovered = gateway.discover()

    assert set(discovered) == ALL_TOOLS
    assert len(READ_TOOLS) == 8


def test_discover_fails_if_required_tool_missing():
    tools = [
        tool
        for tool in valid_tools()
        if tool.name != "mailchimp_ping"
    ]

    with pytest.raises(
        MCPProtocolError,
        match="mailchimp_read_tools_missing",
    ):
        MailchimpGateway(FakeSession(tools=tools)).discover()


def test_discover_rejects_unexpected_tool():
    tools = valid_tools() + [
        SimpleNamespace(
            name="mailchimp_send_campaign",
            input_schema=schema(),
        )
    ]

    with pytest.raises(
        MCPProtocolError,
        match="mailchimp_unexpected_tool_exposed",
    ):
        MailchimpGateway(FakeSession(tools=tools)).discover()


def test_discover_rejects_non_strict_schema():
    tools = valid_tools()
    tools[0] = SimpleNamespace(
        name="mailchimp_ping",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        },
    )

    with pytest.raises(
        MCPProtocolError,
        match="mailchimp_tool_schema_not_strict",
    ):
        MailchimpGateway(FakeSession(tools=tools)).discover()


def test_discover_rejects_unsafe_schema():
    tools = valid_tools()
    tools[0] = SimpleNamespace(
        name="mailchimp_ping",
        input_schema=schema({
            "url": {"type": "string"},
        }),
    )

    with pytest.raises(
        MCPProtocolError,
        match="mailchimp_unsafe_tool_schema",
    ):
        MailchimpGateway(FakeSession(tools=tools)).discover()


def test_invoke_read_calls_allowlisted_tool():
    session = FakeSession()
    gateway = MailchimpGateway(session)
    gateway.discover()

    result = gateway.invoke_read(
        "mailchimp_list_audiences",
        count=3,
        offset=0,
    )

    assert result["ok"] is True
    assert session.calls == [
        (
            "mailchimp_list_audiences",
            {"count": 3, "offset": 0},
        )
    ]


def test_invoke_read_rejects_non_read_tool():
    gateway = MailchimpGateway(FakeSession())
    gateway.discover()

    with pytest.raises(
        MailchimpGatewayError,
        match="mailchimp_read_tool_denied",
    ):
        gateway.invoke_read("mailchimp_send_campaign")


def test_invoke_read_rejects_unknown_argument():
    gateway = MailchimpGateway(FakeSession())
    gateway.discover()

    with pytest.raises(
        MailchimpGatewayError,
        match="mailchimp_argument_not_allowlisted",
    ):
        gateway.invoke_read(
            "mailchimp_list_campaigns",
            count=3,
            arbitrary="no",
        )


@pytest.mark.parametrize("tool,arguments", [
    ("mailchimp_list_members", {"list_id": "aud_123", "count": 0}),
    ("mailchimp_list_segments", {"list_id": "aud_123", "offset": -1}),
    ("mailchimp_list_tags", {"list_id": "bad/id"}),
    ("mailchimp_list_member_tags", {"list_id": "aud_123", "subscriber_hash": "not-a-hash"}),
    ("mailchimp_get_campaign_content", {"campaign_id": "bad/id"}),
])
def test_invoke_read_rejects_invalid_semantic_arguments(tool, arguments):
    gateway = MailchimpGateway(FakeSession())
    gateway.discover()
    with pytest.raises(MailchimpGatewayError, match="mailchimp_argument_invalid"):
        gateway.invoke_read(tool, **arguments)


@pytest.mark.parametrize(
    "field",
    ["side_effects", "writes", "sends"],
)
def test_read_rejects_reported_effect(field):
    result = {
        "ok": True,
        "side_effects": 0,
        "writes": 0,
        "sends": 0,
    }
    result[field] = 1

    gateway = MailchimpGateway(
        FakeSession(result=result)
    )
    gateway.discover()

    with pytest.raises(
        MCPProtocolError,
        match=f"mailchimp_read_reported_{field}",
    ):
        gateway.invoke_read("mailchimp_ping")
