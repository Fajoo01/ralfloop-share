from datetime import datetime, timezone

from ralfloop_agent.unified_assistant.event_router import (
    EventDecision, EventOrigin, EventRouter, RoutedEvent, event_id,
)
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.observability import OperationalMetrics
from ralfloop_agent.unified_assistant.platform import SourceRef


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def event(event_type="UNKNOWN_EVENT", payload=None):
    payload = payload or {"value": 1}
    return RoutedEvent(
        event_id=event_id("fixture", "source-1", event_type, payload),
        event_type=event_type, origin=EventOrigin.POLLER,
        source="fixture", source_id="source-1", occurred_at=NOW, observed_at=NOW,
        entity_refs=("entity.one",), payload=payload,
        provenance=(SourceRef(system="fixture", native_id="source-1", locator="fixture:1", observed_at=NOW.isoformat()),),
    )


def test_unknown_events_store_without_agent_wake(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        metrics = OperationalMetrics()
        result = EventRouter(memory, metrics=metrics).route(event())
        assert result.decision is EventDecision.STORE_ONLY
        assert memory.timeline("entity.one")
        assert metrics.snapshot() == {"event_received": 1}


def test_event_dedup_is_idempotent_and_never_reexecutes(tmp_path):
    calls = []
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        router = EventRouter(
            memory, enabled_workflows=("bandi.review",),
            workflow_handlers={"bandi.review": calls.append},
        )
        first = router.route(event("BANDO_DISCOVERED"))
        second = router.route(event("BANDO_DISCOVERED"))
        assert first.decision is EventDecision.RUN_WORKFLOW
        assert second.decision is EventDecision.IGNORE and second.duplicate
        assert len(calls) == 1
        assert router.metrics.snapshot()["event_deduplicated"] == 1


def test_workflow_and_wake_fail_closed_when_handlers_disabled(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        router = EventRouter(memory)
        assert router.route(event("BANDO_UPDATED")).decision is EventDecision.STORE_ONLY
        assert router.route(event("PRACTICE_STALE", {"value": 2})).decision is EventDecision.STORE_ONLY
        assert router.metrics.snapshot().get("event_wake_agent", 0) == 0


def test_explicit_wake_handler_only_runs_for_allowlisted_rule(tmp_path):
    wakes = []
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        router = EventRouter(memory, wake_handler=wakes.append)
        routed = router.route(event("SOURCE_INCONSISTENCY"))
        assert routed.decision is EventDecision.WAKE_AGENT
        assert len(wakes) == 1
        assert router.metrics.snapshot()["event_wake_agent"] == 1


def test_notify_decision_never_sends_directly(tmp_path):
    from ralfloop_agent.unified_assistant.event_router import EventRule
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        router = EventRouter(memory, rules=(EventRule(event_types=("NOTICE",), decision=EventDecision.NOTIFY),))
        result = router.route(event("NOTICE"))
        assert result.decision is EventDecision.STORE_ONLY
        assert result.reason == "notification_requires_separate_policy"
