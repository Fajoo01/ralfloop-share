from __future__ import annotations

import json

from ralfloop_agent.integration.abc_relation_read import (
    ABCReadResolution,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
)
from ralfloop_agent.nodes import reasoning


class _AvailableAdapter:
    def resolve(self, user_goal: str) -> ABCReadResolution:
        return ABCReadResolution(
            available=True,
            context={
                "state": {"snapshot": "current"},
                "analysis": {"score": 61},
                "references": [{"framework": "dialogo_strategico"}],
            },
        )


class _UnavailableAdapter:
    def resolve(self, user_goal: str) -> ABCReadResolution:
        return ABCReadResolution(
            available=False,
            context={},
            fallback_skills=("abc_memory", "abc_relcalc"),
            error="RuntimeError: broker unavailable",
        )


def test_read_tool_allowlist_is_disjoint_from_local_writes():
    assert READ_ONLY_TOOLS
    assert WRITE_TOOLS == {"abc_record_event", "abc_create_snapshot"}
    assert READ_ONLY_TOOLS.isdisjoint(WRITE_TOOLS)


def test_reasoning_cycle_uses_canonical_abc_read_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("RALF_SANDBOX_PATH", str(tmp_path))
    monkeypatch.setenv("RALF_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(reasoning, "ABCRelationReadAdapter", lambda: _AvailableAdapter())

    envelope = reasoning.run_capability_reasoning_cycle(
        "controlla la dinamica relazionale e i manuali di psicologia",
        {"task_id": "abc-runtime-test"},
    )

    assert envelope.answer == "read_mcp evidence collected"
    assert envelope.evidence is not None
    assert envelope.evidence.command == "mcp:abc_relation:read_only"
    assert envelope.evidence.exit_code == 0
    payload = json.loads(envelope.evidence.stdout or "{}")
    assert payload["source"] == "abc_relation_mcp_read_only"
    assert payload["available"] is True
    assert envelope.meta["read_mcp"] == "abc_relation"
    assert envelope.meta["read_mcp_available"] is True


def test_reasoning_cycle_does_not_fake_shell_evidence_when_abc_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("RALF_SANDBOX_PATH", str(tmp_path))
    monkeypatch.setenv("RALF_AUDIT_PATH", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(reasoning, "ABCRelationReadAdapter", lambda: _UnavailableAdapter())

    envelope = reasoning.run_capability_reasoning_cycle(
        "controlla la strategia relazionale",
        {"task_id": "abc-runtime-down"},
    )

    assert envelope.answer == "read_mcp unavailable"
    assert envelope.evidence is not None
    assert envelope.evidence.command == "mcp:abc_relation:read_only"
    assert envelope.evidence.exit_code == 1
    assert envelope.meta["read_mcp_available"] is False
    assert envelope.meta["fallback_skills"] == ["abc_memory", "abc_relcalc"]
