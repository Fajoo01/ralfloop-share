from types import SimpleNamespace

import pytest

from src.mailchimp import (
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
        "count": {"type": "integer"},
        "offset": {"type": "integer"},
    }
    return [
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
    ]


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


def test_discover_accepts_exact_read_only_contract():
    gateway = MailchimpGateway(FakeSession())

    discovered = gateway.discover()

    assert set(discovered) == READ_TOOLS


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
