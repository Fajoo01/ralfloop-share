from __future__ import annotations

import pytest

from ralfloop_agent.unified_assistant.browser_mcp_adapter import BrowserMCPReadOnly
from src.mcp_transport import MCPTool


class FakeSession:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def list_tools(self):
        return (
            MCPTool("browser_tabs", "tabs", {"type": "object"}),
            MCPTool("browser_snapshot", "snapshot", {"type": "object"}),
            MCPTool("browser_run_code_unsafe", "unsafe", {"type": "object"}),
        )

    def call_tool(self, tool, arguments):
        self.calls.append((tool, dict(arguments)))
        return {"content": [{"type": "text", "text": f"read via {tool}"}]}


def test_browser_tabs_is_fixed_to_read_only_list():
    calls = []
    browser = BrowserMCPReadOnly(session_factory=lambda: FakeSession(calls))
    result = browser.inspect("mostrami le schede del browser")
    assert calls == [("browser_tabs", {"action": "list"})]
    assert result["operation"] == "tabs"
    assert result["side_effects"] == 0
    assert result["writes"] == 0


def test_browser_snapshot_is_the_default_inspection():
    calls = []
    browser = BrowserMCPReadOnly(session_factory=lambda: FakeSession(calls))
    result = browser.inspect("ispeziona la pagina web nel browser")
    assert calls == [("browser_snapshot", {})]
    assert result["tool"] == "browser_snapshot"


def test_browser_read_adapter_rejects_interaction_language_before_mcp_call():
    calls = []
    browser = BrowserMCPReadOnly(session_factory=lambda: FakeSession(calls))
    with pytest.raises(ValueError, match="browser_interaction_not_read_only"):
        browser.inspect("clicca il pulsante nel browser")
    assert calls == []
