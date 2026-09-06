from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from ralfloop_agent.unified_assistant.pec_runts import PecMessage
from ralfloop_agent.unified_assistant.pec_runts_mcp import capability_descriptors
from ralfloop_agent.unified_assistant.pec_runts_telegram import (
    PecRuntsToolDecision, decision_for_telegram, execute_telegram_read,
)
from ralfloop_agent.unified_assistant.platform import CapabilityRegistry, SourceRef
from ralfloop_agent.unified_assistant.runtime import unified_route_probe


TEXT = "Cerca nella PEC la comunicazione relativa alla pratica RUNTS 2603942 e dimmi cosa trovi. Usa solo i tuoi tool disponibili e non eseguire azioni di scrittura."
NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


class PecProvider:
    calls = 0

    def list_messages(self, *, limit):
        self.calls += 1
        return (PecMessage.build(
            native_id="synthetic-pec-1", subject="Synthetic RUNTS notification",
            sender="synthetic@example.invalid", received_at=NOW, observed_at=NOW,
            body="Synthetic read-only evidence", unread=True, certified=True,
            runts_reference="2603942",
            source=SourceRef(system="pec", native_id="synthetic-pec-1", locator="https://example.invalid/pec/synthetic-pec-1", observed_at=NOW.isoformat(), content_hash="1" * 64),
        ),)

    def get_message(self, native_id):
        return self.list_messages(limit=1)[0]


def test_exact_telegram_failure_regression_routes_rag_validates_and_invokes_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    decision = decision_for_telegram(TEXT)
    assert decision is not None and decision.model_dump() == {
        "action": "tool", "response": None,
        "tool_id": "pec_find_by_runts_reference",
        "arguments": {"runts_reference": "2603942", "limit": 100},
    }
    selected = CapabilityRegistry(capability_descriptors()).retrieve(TEXT, domains=("pec_runts",), limit=3)
    assert selected[0].capability_id == "pec_find_by_runts_reference"
    assert all(row.domain == "pec_runts" for row in selected)

    route = unified_route_probe(TEXT, {"source": "telegram_natural"})
    assert route["task_mode"] == "tool_backed_read"
    assert route["mcp_used"] == ["pec_runts.mcp"]

    provider = PecProvider()
    result = execute_telegram_read(TEXT, memory_path=tmp_path / "memory.sqlite", pec_provider=provider)
    assert result["metadata"]["tool_decision_valid"] is True
    assert result["metadata"]["selected_capability"] == "pec_find_by_runts_reference"
    assert result["metadata"]["mcp_invoked"] is True and result["tools_executed"] is True
    assert result["metadata"]["read_result"]["messages"][0]["runts_reference"] == "2603942"
    assert result["metadata"]["writes"] == 0 and provider.calls == 1


def test_telegram_decision_schema_remains_strict():
    with pytest.raises(ValidationError):
        PecRuntsToolDecision.model_validate({
            "action": "tool", "response": None,
            "tool_id": "pec_find_by_runts_reference",
            "arguments": {"runts_reference": "2603942", "write": False},
        })
