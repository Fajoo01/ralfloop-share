from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Any, Callable, Iterable

from pydantic import Field

from .contracts import StrictModel
from .memory_service import MemoryEvent, MemoryService
from .observability import OperationalMetrics
from .platform import SourceRef


class EventOrigin(StrEnum):
    POLLER = "POLLER"
    WEBHOOK = "WEBHOOK"
    SCHEDULER = "SCHEDULER"
    API_WATCHER = "API_WATCHER"
    SERVICE = "SERVICE"
    MCP_SUBSCRIPTION = "MCP_SUBSCRIPTION"


class EventDecision(StrEnum):
    IGNORE = "IGNORE"
    STORE_ONLY = "STORE_ONLY"
    UPDATE_PRACTICE = "UPDATE_PRACTICE"
    RUN_WORKFLOW = "RUN_WORKFLOW"
    WAKE_AGENT = "WAKE_AGENT"
    NOTIFY = "NOTIFY"


class RoutedEvent(StrictModel):
    event_id: str = Field(pattern=r"^event\.[a-z0-9][a-z0-9_.-]{0,118}$")
    event_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,95}$")
    origin: EventOrigin
    source: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    source_id: str = Field(min_length=1, max_length=500)
    occurred_at: datetime
    observed_at: datetime
    entity_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance: tuple[SourceRef, ...] = Field(min_length=1, max_length=16)

    def memory_event(self) -> MemoryEvent:
        return MemoryEvent.build(
            event_id=self.event_id, type=self.event_type, source=self.source,
            source_id=self.source_id, occurred_at=self.occurred_at,
            observed_at=self.observed_at, entity_refs=self.entity_refs,
            payload={"origin": self.origin, **self.payload}, provenance=self.provenance,
        )


class EventRule(StrictModel):
    event_types: tuple[str, ...] = Field(min_length=1, max_length=64)
    decision: EventDecision
    workflow: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,95}$")


class EventRouteResult(StrictModel):
    event_id: str
    decision: EventDecision
    stored: bool
    duplicate: bool = False
    workflow: str | None = None
    reason: str


DEFAULT_RULES = (
    EventRule(event_types=("BANDO_DISCOVERED", "BANDO_UPDATED", "BANDO_DEADLINE_CHANGED", "BANDO_DOCUMENT_CHANGED", "BANDO_FAQ_CHANGED"), decision=EventDecision.RUN_WORKFLOW, workflow="bandi.review"),
    EventRule(event_types=("BANDO_CLOSED",), decision=EventDecision.UPDATE_PRACTICE, workflow="bandi.close"),
    EventRule(event_types=("MEDIA_TICKET_CREATED", "MEDIA_LOW_QUALITY_DETECTED", "MEDIA_LANGUAGE_MISSING", "MEDIA_PROBE_FAILED"), decision=EventDecision.RUN_WORKFLOW, workflow="media.triage"),
    EventRule(event_types=("MEDIA_FIX_PROPOSED",), decision=EventDecision.UPDATE_PRACTICE, workflow="media.review_fix"),
    EventRule(event_types=("SOURCE_INCONSISTENCY", "PRACTICE_STALE"), decision=EventDecision.WAKE_AGENT),
    EventRule(event_types=("PEC_MESSAGE_DISCOVERED", "RUNTS_MESSAGE_DISCOVERED", "RUNTS_ATTACHMENT_DISCOVERED", "RUNTS_AUTH_REQUIRED"), decision=EventDecision.STORE_ONLY),
    EventRule(event_types=("PEC_RUNTS_NOTIFICATION",), decision=EventDecision.RUN_WORKFLOW, workflow="runts.correlate"),
    EventRule(event_types=("RUNTS_PRACTICE_UPDATED",), decision=EventDecision.RUN_WORKFLOW, workflow="runts.review"),
    EventRule(event_types=("RUNTS_ACTION_REQUIRED",), decision=EventDecision.WAKE_AGENT),
    EventRule(event_types=("EMAIL_RECEIVED",), decision=EventDecision.STORE_ONLY),
)


class EventRouter:
    def __init__(
        self, memory: MemoryService, *, rules: Iterable[EventRule] = DEFAULT_RULES,
        enabled_workflows: Iterable[str] = (),
        workflow_handlers: dict[str, Callable[[RoutedEvent], None]] | None = None,
        wake_handler: Callable[[RoutedEvent], None] | None = None,
        metrics: OperationalMetrics | None = None,
    ) -> None:
        self.memory = memory
        self.rules = tuple(rules)
        self.enabled_workflows = frozenset(enabled_workflows)
        self.workflow_handlers = workflow_handlers or {}
        self.wake_handler = wake_handler
        self.metrics = metrics or OperationalMetrics()
        seen: set[str] = set()
        for rule in self.rules:
            overlap = seen & set(rule.event_types)
            if overlap:
                raise ValueError("event_rule_conflict")
            seen.update(rule.event_types)

    def route(self, event: RoutedEvent) -> EventRouteResult:
        self.metrics.increment("event_received")
        stored = self.memory.append_event(event.memory_event())
        if not stored:
            self.metrics.increment("event_deduplicated")
            return EventRouteResult(event_id=event.event_id, decision=EventDecision.IGNORE, stored=False, duplicate=True, reason="duplicate_event")
        rule = next((row for row in self.rules if event.event_type in row.event_types), None)
        if rule is None:
            return EventRouteResult(event_id=event.event_id, decision=EventDecision.STORE_ONLY, stored=True, reason="no_matching_rule")
        decision = rule.decision
        if decision in {EventDecision.RUN_WORKFLOW, EventDecision.UPDATE_PRACTICE}:
            if not rule.workflow or rule.workflow not in self.enabled_workflows:
                return EventRouteResult(event_id=event.event_id, decision=EventDecision.STORE_ONLY, stored=True, workflow=rule.workflow, reason="workflow_disabled")
            handler = self.workflow_handlers.get(rule.workflow)
            if handler is None:
                return EventRouteResult(event_id=event.event_id, decision=EventDecision.STORE_ONLY, stored=True, workflow=rule.workflow, reason="workflow_handler_unavailable")
            handler(event)
            self.metrics.increment("event_workflow_run")
        elif decision is EventDecision.WAKE_AGENT:
            if self.wake_handler is None:
                return EventRouteResult(event_id=event.event_id, decision=EventDecision.STORE_ONLY, stored=True, reason="wake_handler_unavailable")
            self.wake_handler(event)
            self.metrics.increment("event_wake_agent")
        elif decision is EventDecision.NOTIFY:
            return EventRouteResult(event_id=event.event_id, decision=EventDecision.STORE_ONLY, stored=True, reason="notification_requires_separate_policy")
        return EventRouteResult(event_id=event.event_id, decision=decision, stored=True, workflow=rule.workflow, reason="rule_applied")


def event_id(source: str, source_id: str, event_type: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps({"source": source, "source_id": source_id, "event_type": event_type, "payload": payload}, sort_keys=True, separators=(",", ":"), default=str)
    return "event." + source + "-" + hashlib.sha256(canonical.encode()).hexdigest()[:24]


__all__ = ["DEFAULT_RULES", "EventDecision", "EventOrigin", "EventRouteResult", "EventRouter", "EventRule", "RoutedEvent", "event_id"]
