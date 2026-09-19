from __future__ import annotations

import json

from ralfloop_agent.integration.read_mcp import (
    ABC_RELATION_READ_TOOLS,
    ABC_RELATION_WRITE_TOOLS,
    read_abc_relation,
)
from ralfloop_agent.integration.capability_adapter import route_task
from ralfloop_agent.nodes import reasoning
from src.models import Evidence


class FakeRelationClient:
    socket_path = "/tmp/fake-abc.sock"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_state(self):
        self.calls.append("abc_get_state")
        return {"snapshot": "current"}

    def analyze(self):
        self.calls.append("abc_analyze")
        return {"score": 61}

    def timeline(self, *, limit=100, kind=None):
        self.calls.append("abc_get_timeline")
        return [{"event_id": "abc_evt_test"}]

    def references(self):
        self.calls.append("abc_get_reference_library")
        return [{"framework": "dialogo_strategico"}]


def test_read_tool_allowlist_is_disjoint_from_local_writes():
    assert ABC_RELATION_READ_TOOLS
    assert ABC_RELATION_WRITE_TOOLS == {"abc_record_event", "abc_create_snapshot"}
    assert ABC_RELATION_READ_TOOLS.isdisjoint(ABC_RELATION_WRITE_TOOLS)


def test_router_selects_abc_relation_as_read_connector():
    route = route_task("analizza la strategia relazionale e la curva relazionale")

    assert route.mode == "check_only"
    assert "abc_relation" in route.mcp_used
    assert route.requires_confirmation is False


def test_abc_read_collects_state_analysis_and_optional_frameworks_without_writes():
    client = FakeRelationClient()

    evidence = read_abc_relation(
        "controlla la timeline e usa i manuali di psicologia e dialogo strategico",
        client=client,
    )

    payload = json.loads(evidence.stdout or "{}")
    assert evidence.command == "mcp:abc_relation:read"
    assert evidence.path == client.socket_path
    assert evidence.exit_code == 0
    assert payload["side_effects"] == 0
    assert payload["writes"] == 0
    assert payload["sends"] == 0
    assert payload["state"]["snapshot"] == "current"
    assert payload["analysis"]["score"] == 61
    assert payload["timeline"]
    assert payload["references"]
    assert set(client.calls).issubset(ABC_RELATION_READ_TOOLS)
    assert not set(client.calls).intersection(ABC_RELATION_WRITE_TOOLS)


def test_reasoning_cycle_uses_selected_read_mcp_instead_of_shell(monkeypatch, tmp_path):
    monkeypatch.setenv("RALF_SANDBOX_PATH", str(tmp_path))
    monkeypatch.setenv("RALF_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    calls: list[tuple[str, str]] = []

    def fake_read_mcp(connector: str, user_goal: str) -> Evidence:
        calls.append((connector, user_goal))
        return Evidence(
            command="mcp:abc_relation:read",
            path="/run/ralf-abc-relation-mcp/mcp.sock",
            exit_code=0,
            stdout='{"writes": 0, "sends": 0}',
        )

    monkeypatch.setattr(reasoning, "run_read_mcp", fake_read_mcp)

    envelope = reasoning.run_capability_reasoning_cycle(
        "controlla la dinamica relazionale",
        {"task_id": "abc-runtime-test"},
    )

    assert calls == [("abc_relation", "controlla la dinamica relazionale")]
    assert envelope.answer == "read_mcp evidence collected"
    assert envelope.evidence is not None
    assert envelope.evidence.command == "mcp:abc_relation:read"
    assert envelope.meta["read_mcp"] == "abc_relation"
