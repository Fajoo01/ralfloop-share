from __future__ import annotations

from datetime import datetime, timedelta, timezone

from ralfloop_agent.unified_assistant.bandi_mcp import BandiMCPServer, TOOLS
from ralfloop_agent.unified_assistant.bandi_service import (
    BandiService, BandoAttachment, BandoRequirements, BandoStatus,
    EligibilityOutcome, NormalizedBando, TiremmEligibilityProfile,
)
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.platform import SourceRef


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def source(native_id="grant-1"):
    return SourceRef(
        system="comune_milano", native_id=native_id,
        locator=f"https://example.invalid/{native_id}", observed_at=NOW.isoformat(),
    )


def bando(**changes):
    values = {
        "source": "comune_milano", "source_id": "grant-1",
        "title": "Synthetic youth activities grant", "issuer": "Synthetic authority",
        "territory": ("Milano",), "beneficiary_types": ("APS", "ETS"),
        "opening_at": NOW - timedelta(days=2), "deadline_at": NOW + timedelta(days=30),
        "budget_total": 100000.0, "grant_min": 5000.0, "grant_max": 20000.0,
        "cofinancing": "none", "requirements": BandoRequirements(
            aps_allowed=True, ets_runts_required=True,
            eligible_territories=("Milano",), partnership_mandatory=False,
            cofinancing_required=False,
        ),
        "attachments": (), "status": BandoStatus.OPEN, "source_ref": source(),
        "first_seen": NOW, "last_seen": NOW, "changed_at": NOW,
    }
    values.update(changes)
    return NormalizedBando.build(**values)


def test_bandi_ingest_persists_timeline_and_detects_deadline_document_faq_close(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        service = BandiService(memory)
        first = bando()
        assert [row.type for row in service.ingest((first,))] == ["BANDO_DISCOVERED"]
        assert service.ingest((first,)) == ()
        attachments = (
            BandoAttachment(attachment_id="doc-1", title="Call", kind="document", source_ref=source("doc-1"), content_hash="1" * 64),
            BandoAttachment(attachment_id="faq-1", title="FAQ", kind="faq", source_ref=source("faq-1"), content_hash="2" * 64),
        )
        changed = bando(
            deadline_at=NOW + timedelta(days=40), attachments=attachments,
            status=BandoStatus.CLOSED, last_seen=NOW + timedelta(hours=1),
            changed_at=NOW + timedelta(hours=1),
        )
        event_types = {row.type for row in service.ingest((changed,))}
        assert event_types == {
            "BANDO_UPDATED", "BANDO_DEADLINE_CHANGED", "BANDO_DOCUMENT_CHANGED",
            "BANDO_FAQ_CHANGED", "BANDO_CLOSED",
        }
        assert service.get(first.entity_id) == changed
        assert len(service.changes(first.entity_id)) == 6


def test_bandi_survives_memory_restart(tmp_path):
    path = tmp_path / "memory.sqlite"
    with MemoryService(path) as memory:
        BandiService(memory).ingest((bando(),))
    with MemoryService(path) as memory:
        restored = BandiService(memory).get(bando().entity_id)
        assert restored and restored.title == "Synthetic youth activities grant"


def test_deterministic_eligibility_filters_before_llm():
    profile = TiremmEligibilityProfile()
    assert BandiService.evaluate(bando(), profile, now=NOW).outcome is EligibilityOutcome.ELIGIBLE
    excluded = bando(requirements=BandoRequirements(aps_allowed=False))
    assert BandiService.evaluate(excluded, profile, now=NOW).outcome is EligibilityOutcome.INELIGIBLE
    expired = bando(deadline_at=NOW - timedelta(days=1), opening_at=NOW - timedelta(days=10))
    assert "DEADLINE_PASSED" in BandiService.evaluate(expired, profile, now=NOW).reasons
    ambiguous = bando(requirements=BandoRequirements(aps_allowed=None, partnership_mandatory=True))
    result = BandiService.evaluate(ambiguous, profile, now=NOW)
    assert result.outcome is EligibilityOutcome.AMBIGUOUS
    assert result.llm_review_required is True


def test_source_adapter_poll_order_is_deterministic(tmp_path):
    calls = []

    class Adapter:
        def __init__(self, identity, priority):
            self.source_id, self.source_priority = identity, priority

        def fetch(self):
            calls.append(self.source_id)
            return ()

    with MemoryService(tmp_path / "memory.sqlite") as memory:
        BandiService(memory).poll((Adapter("html", 5), Adapter("api", 1), Adapter("rss", 4)))
    assert calls == ["api", "rss", "html"]


def test_bandi_mcp_exposes_eight_semantic_source_backed_tools(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        service = BandiService(memory)
        row = bando()
        service.ingest((row,))
        server = BandiMCPServer(service)
        names = {tool["name"] for tool in server.list_tools()}
        assert names == set(TOOLS)
        assert not any("raw" in name or "request" in name or "execute" in name for name in names)
        assert server.call("bandi_list_open", {})["structuredContent"]["items"][0]["source_ref"]
        found = server.call("bandi_find_for_tiremm", {})["structuredContent"]["items"][0]
        assert found["eligibility"]["outcome"] == "ELIGIBLE"
        assert server.call("bandi_get_changes", {"bando_id": row.entity_id})["structuredContent"]["events"]
        assert server.call("bandi_get", {"bando_id": row.entity_id, "url": "x"})["isError"]
