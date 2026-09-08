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



@pytest.mark.parametrize(
    "text,expected_limit,expected_mode",
    [
        ("Cosa dice la PEC che è appena arrivata?", 1, "latest"),
        ("Controlla l'ultima PEC", 1, "latest"),
        ("Ci sono nuove PEC?", 100, "unread"),
        ("Leggi la PEC appena arrivata", 1, "latest"),
        ("Leggi la PEC di synthetic@example.invalid", 100, "search"),
    ],
)
def test_generic_latest_pec_phrases_route_to_mcp_read(
    text,
    expected_limit,
    expected_mode,
    monkeypatch,
):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")

    decision = decision_for_telegram(text)
    assert decision is not None
    assert decision.tool_id == "pec_discover_messages"
    assert decision.arguments.model_dump() == {"limit": expected_limit}
    assert decision.mode == expected_mode

    route = unified_route_probe(
        text,
        {"source": "telegram_natural"},
    )

    assert route is not None
    assert route["task_mode"] == "tool_backed_read"
    assert route["interaction_class"] == "TOOL_BACKED_READ"
    assert route["intent"] == "pec.inbox.read"
    assert route["mcp_used"] == ["pec_runts.mcp"]
    assert route["write_policy"] == "no_write"
    assert route["requires_confirmation"] is False


def test_generic_latest_pec_executes_read_only_and_returns_details(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")

    provider = PecProvider()

    result = execute_telegram_read(
        "Cosa dice la PEC che è appena arrivata?",
        memory_path=tmp_path / "memory.sqlite",
        pec_provider=provider,
    )

    assert result["ok"] is True
    assert result["capability"] == "pec_discover_messages"
    assert result["tools_executed"] is True
    assert result["approval_required"] is False

    metadata = result["metadata"]
    assert metadata["mcp_invoked"] is True
    assert metadata["selected_capability"] == "pec_discover_messages"
    assert metadata["writes"] == 0

    answer = result["response"]
    assert "Ultima PEC ricevuta:" in answer
    assert "Mittente: synthetic@example.invalid" in answer
    assert "Oggetto: Synthetic RUNTS notification" in answer
    assert "Cosa comunica: Synthetic read-only evidence" in answer

    assert provider.calls == 1



def test_new_and_sender_filtered_pec_reads(tmp_path, monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")

    older = NOW.replace(day=2)

    class MultiPecProvider:
        calls = 0

        def list_messages(self, *, limit):
            self.calls += 1
            rows = (
                PecMessage.build(
                    native_id="pec-new",
                    subject="Avviso recente",
                    sender="ente@example.invalid",
                    received_at=NOW,
                    observed_at=NOW,
                    body="Inviare la risposta entro 10 giorni.",
                    unread=True,
                    certified=True,
                    source=SourceRef(
                        system="pec",
                        native_id="pec-new",
                        locator="https://example.invalid/pec/new",
                        observed_at=NOW.isoformat(),
                        content_hash="2" * 64,
                    ),
                ),
                PecMessage.build(
                    native_id="pec-old",
                    subject="Comunicazione precedente",
                    sender="synthetic@example.invalid",
                    received_at=older,
                    observed_at=NOW,
                    body="Messaggio precedente.",
                    unread=False,
                    certified=True,
                    source=SourceRef(
                        system="pec",
                        native_id="pec-old",
                        locator="https://example.invalid/pec/old",
                        observed_at=NOW.isoformat(),
                        content_hash="3" * 64,
                    ),
                ),
            )
            return rows[:limit]

        def get_message(self, native_id):
            return next(
                row
                for row in self.list_messages(limit=100)
                if row.native_id == native_id
            )

    provider = MultiPecProvider()

    unread_result = execute_telegram_read(
        "Ci sono nuove PEC?",
        memory_path=tmp_path / "unread.sqlite",
        pec_provider=provider,
    )
    assert unread_result["ok"] is True
    assert "Risultano 1 PEC non lette" in unread_result["response"]
    assert "Mittente: ente@example.invalid" in unread_result["response"]
    assert "entro 10 giorni" in unread_result["response"]
    assert unread_result["metadata"]["writes"] == 0

    search_result = execute_telegram_read(
        "Leggi la PEC di synthetic@example.invalid",
        memory_path=tmp_path / "search.sqlite",
        pec_provider=provider,
    )
    assert search_result["ok"] is True
    assert "Trovate 1 PEC corrispondenti" in search_result["response"]
    assert "Mittente: synthetic@example.invalid" in search_result["response"]
    assert search_result["metadata"]["writes"] == 0



def test_pec_sender_search_preserves_dotted_address():
    decision = decision_for_telegram(
        "Leggi la PEC di synthetic@example.invalid"
    )
    assert decision is not None
    assert decision.tool_id == "pec_discover_messages"
    assert decision.mode == "search"
    assert decision.search_term == "synthetic@example.invalid"



def test_exact_runts_pec_route_keeps_runts_reference_intent(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")

    route = unified_route_probe(
        "Controlla PEC pratica RUNTS 2603942",
        {"source": "telegram_natural"},
    )

    assert route is not None
    assert route["task_mode"] == "tool_backed_read"
    assert route["intent"] == "pec.runts_reference.read"
    assert route["write_policy"] == "no_write"
