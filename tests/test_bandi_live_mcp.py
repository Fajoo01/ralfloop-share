from __future__ import annotations

from ralfloop_agent.unified_assistant.bandi_live_mcp import (
    ALL_TOOLS, BandiLiveMCPContext,
)
from ralfloop_agent.unified_assistant.contracts import PlanAssignment, PolicyClass
from ralfloop_agent.unified_assistant.safe_mcp_read_adapters import bandi_discovery_adapter
from src.mcp_transport import MCPTool


class FakeSession:
    def __init__(self, path: str, timeout: float):
        self.path = path
        self.timeout = timeout
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def list_tools(self):
        return tuple(MCPTool(name, "semantic", {"type": "object"}) for name in ALL_TOOLS)
    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "bandi_search_latest":
            payload = {"ok": True, "status": "completed", "items": []}
        else:
            payload = {
                "ok": True,
                "status": "completed_partial",
                "items": [{
                    "call_key": "abc12345",
                    "title": "Bando cultura",
                    "issuer": "Regione Lombardia",
                    "deadline": "2026-10-30",
                    "primary_url": "https://example.invalid/bando",
                    "priority": "HIGH",
                }],
                "freshness": {"fresh": True},
            }
        return {"structuredContent": payload, "isError": False}


def _assignment() -> PlanAssignment:
    return PlanAssignment(
        task_id="task.bandi", domain="bandi", skill="bandi.discovery",
        objective="bandi aperti per Tiremm", input_refs=("user.goal",),
        output_ref="artifact.bandi", policy=PolicyClass.READ,
    )


def test_live_context_discovers_exact_contract_and_reads_without_refresh():
    created = []

    def factory(path, timeout):
        session = FakeSession(path, timeout)
        created.append(session)
        return session

    with BandiLiveMCPContext(session_factory=factory) as client:
        result = client.latest(limit=3)

    assert result["status"] == "completed_partial"
    assert created[0].calls == [("bandi_latest", {"limit": 3})]
    assert not any(name == "bandi_research_now" for name, _ in created[0].calls)


def test_bandi_discovery_adapter_falls_back_to_latest_without_refresh(monkeypatch):
    session = FakeSession("/tmp/test.sock", 8.0)
    monkeypatch.setattr(
        "ralfloop_agent.unified_assistant.safe_mcp_read_adapters.BandiLiveMCPContext",
        lambda timeout=8.0: BandiLiveMCPContext(
            socket_path="/tmp/test.sock",
            timeout=timeout,
            session_factory=lambda _path, _timeout: session,
        ),
    )

    artifact = bandi_discovery_adapter(_assignment(), {})

    assert artifact.status == "completed"
    assert artifact.facts[0]["title"] == "Bando cultura"
    assert artifact.payload["side_effects"] == 0
    assert artifact.payload["writes"] == 0
    assert artifact.payload["sends"] == 0
    names = [name for name, _args in session.calls]
    assert names == ["bandi_search_latest", "bandi_latest"]
    assert "bandi_research_now" not in names
