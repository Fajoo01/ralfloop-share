from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Iterable, Protocol

from pydantic import Field, model_validator

from .contracts import StrictModel
from .memory_service import MemoryEntity, MemoryEvent, MemoryService
from .platform import SourceRef


class BandoStatus(StrEnum):
    UPCOMING = "UPCOMING"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"


class BandoEventType(StrEnum):
    DISCOVERED = "BANDO_DISCOVERED"
    UPDATED = "BANDO_UPDATED"
    DEADLINE_CHANGED = "BANDO_DEADLINE_CHANGED"
    DOCUMENT_CHANGED = "BANDO_DOCUMENT_CHANGED"
    FAQ_CHANGED = "BANDO_FAQ_CHANGED"
    CLOSED = "BANDO_CLOSED"


class BandoAttachment(StrictModel):
    attachment_id: str = Field(min_length=1, max_length=240)
    title: str = Field(min_length=1, max_length=500)
    source_ref: SourceRef
    kind: str = Field(default="document", pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class BandoRequirements(StrictModel):
    aps_allowed: bool | None = None
    ets_runts_required: bool | None = None
    eligible_territories: tuple[str, ...] = ()
    partnership_mandatory: bool | None = None
    cofinancing_required: bool | None = None
    minimum_budget: float | None = Field(default=None, ge=0)


class NormalizedBando(StrictModel):
    source: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    source_id: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=1000)
    issuer: str = Field(min_length=1, max_length=500)
    territory: tuple[str, ...] = ()
    beneficiary_types: tuple[str, ...] = ()
    opening_at: datetime | None = None
    deadline_at: datetime | None = None
    budget_total: float | None = Field(default=None, ge=0)
    grant_min: float | None = Field(default=None, ge=0)
    grant_max: float | None = Field(default=None, ge=0)
    cofinancing: str | None = Field(default=None, max_length=500)
    requirements: BandoRequirements
    attachments: tuple[BandoAttachment, ...] = ()
    status: BandoStatus
    source_ref: SourceRef
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    first_seen: datetime
    last_seen: datetime
    changed_at: datetime

    @property
    def entity_id(self) -> str:
        return "bando." + hashlib.sha256(f"{self.source}|{self.source_id}".encode()).hexdigest()[:32]

    @model_validator(mode="after")
    def validate_ranges(self) -> "NormalizedBando":
        if self.grant_min is not None and self.grant_max is not None and self.grant_min > self.grant_max:
            raise ValueError("bando_grant_range_invalid")
        if self.opening_at and self.deadline_at and self.opening_at > self.deadline_at:
            raise ValueError("bando_date_range_invalid")
        return self

    @classmethod
    def build(cls, **values):
        content = {key: value for key, value in values.items() if key not in {"content_hash", "first_seen", "last_seen", "changed_at"}}
        encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
        return cls(content_hash=hashlib.sha256(encoded.encode()).hexdigest(), **values)


class SourceAdapter(Protocol):
    source_id: str
    source_priority: int

    def fetch(self) -> Iterable[NormalizedBando]: ...


class EligibilityOutcome(StrEnum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    AMBIGUOUS = "AMBIGUOUS"


class TiremmEligibilityProfile(StrictModel):
    legal_form: str = "APS"
    runts_registered: bool = True
    territories: tuple[str, ...] = ("Milano", "Lombardia", "Italia")
    maximum_project_budget: float | None = Field(default=None, ge=0)
    partnership_available: bool | None = None
    cofinancing_available: bool | None = None


class BandoEligibility(StrictModel):
    outcome: EligibilityOutcome
    reasons: tuple[str, ...]
    deterministic: bool = True
    llm_review_required: bool = False


class BandiService:
    def __init__(self, memory: MemoryService) -> None:
        self.memory = memory

    def ingest(self, rows: Iterable[NormalizedBando]) -> tuple[MemoryEvent, ...]:
        emitted: list[MemoryEvent] = []
        for incoming in rows:
            previous_entity = self.memory.get_entity(incoming.entity_id)
            previous = NormalizedBando.model_validate(previous_entity.data) if previous_entity else None
            if previous and previous.content_hash == incoming.content_hash:
                continue
            event_types = self._changes(previous, incoming)
            for event_type in event_types:
                payload = {
                    "bando_id": incoming.entity_id, "event_type": event_type,
                    "previous_hash": previous.content_hash if previous else None,
                    "content_hash": incoming.content_hash,
                }
                event = MemoryEvent.build(
                    event_id="event.bando-" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24],
                    type=event_type, source=incoming.source, source_id=incoming.source_id,
                    occurred_at=incoming.changed_at, observed_at=incoming.last_seen,
                    entity_refs=(incoming.entity_id,), payload=payload,
                    provenance=(incoming.source_ref,),
                )
                self.memory.append_event(event)
                emitted.append(event)
            self.memory.put_entity(MemoryEntity.build(
                entity_id=incoming.entity_id, domain="bandi", entity_type="BANDO",
                status=incoming.status, updated_at=incoming.last_seen,
                data=incoming.model_dump(mode="json"), provenance=(incoming.source_ref,),
            ))
        return tuple(emitted)

    def poll(self, adapters: Iterable[SourceAdapter]) -> tuple[MemoryEvent, ...]:
        events = []
        for adapter in sorted(adapters, key=lambda row: row.source_priority):
            events.extend(self.ingest(adapter.fetch()))
        return tuple(events)

    def get(self, bando_id: str) -> NormalizedBando | None:
        row = self.memory.get_entity(bando_id)
        return NormalizedBando.model_validate(row.data) if row and row.domain == "bandi" else None

    def list_open(self, *, now: datetime, limit: int = 100) -> tuple[NormalizedBando, ...]:
        rows = (NormalizedBando.model_validate(row.data) for row in self.memory.list_entities(domain="bandi", limit=limit))
        return tuple(row for row in rows if row.status is BandoStatus.OPEN and (row.deadline_at is None or row.deadline_at >= now))

    def search(self, query: str, *, limit: int = 20) -> tuple[NormalizedBando, ...]:
        terms = tuple(term.casefold() for term in query.split() if len(term) > 2)
        rows = (NormalizedBando.model_validate(row.data) for row in self.memory.list_entities(domain="bandi", limit=100))
        scored = []
        for row in rows:
            text = " ".join((row.title, row.issuer, *row.territory, *row.beneficiary_types)).casefold()
            score = sum(term in text for term in terms)
            if score:
                scored.append((score, row.deadline_at or datetime.max.replace(tzinfo=timezone.utc), row))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(item[2] for item in scored[:limit])

    def changes(self, bando_id: str) -> tuple[MemoryEvent, ...]:
        return self.memory.timeline(bando_id)

    @staticmethod
    def evaluate(row: NormalizedBando, profile: TiremmEligibilityProfile, *, now: datetime) -> BandoEligibility:
        failed: list[str] = []
        unknown: list[str] = []
        requirements = row.requirements
        if requirements.aps_allowed is False:
            failed.append("APS_EXCLUDED")
        elif requirements.aps_allowed is None:
            unknown.append("APS_UNCLEAR")
        if requirements.ets_runts_required and not profile.runts_registered:
            failed.append("RUNTS_REQUIRED")
        if row.deadline_at and row.deadline_at < now:
            failed.append("DEADLINE_PASSED")
        if requirements.eligible_territories and not set(map(str.casefold, requirements.eligible_territories)) & set(map(str.casefold, profile.territories)):
            failed.append("TERRITORY_EXCLUDED")
        if requirements.partnership_mandatory and profile.partnership_available is not True:
            (unknown if profile.partnership_available is None else failed).append("PARTNERSHIP_REQUIRED")
        if requirements.cofinancing_required and profile.cofinancing_available is not True:
            (unknown if profile.cofinancing_available is None else failed).append("COFINANCING_REQUIRED")
        if profile.maximum_project_budget is not None and row.grant_min is not None and row.grant_min > profile.maximum_project_budget:
            failed.append("BUDGET_INCOMPATIBLE")
        if failed:
            return BandoEligibility(outcome=EligibilityOutcome.INELIGIBLE, reasons=tuple(failed))
        if unknown:
            return BandoEligibility(outcome=EligibilityOutcome.AMBIGUOUS, reasons=tuple(unknown), llm_review_required=True)
        return BandoEligibility(outcome=EligibilityOutcome.ELIGIBLE, reasons=("DETERMINISTIC_FILTER_PASS",))

    @staticmethod
    def _changes(previous: NormalizedBando | None, current: NormalizedBando) -> tuple[str, ...]:
        if previous is None:
            return (BandoEventType.DISCOVERED,)
        output: list[str] = [BandoEventType.UPDATED]
        if previous.deadline_at != current.deadline_at:
            output.append(BandoEventType.DEADLINE_CHANGED)
        old_docs = {(row.attachment_id, row.content_hash) for row in previous.attachments if row.kind != "faq"}
        new_docs = {(row.attachment_id, row.content_hash) for row in current.attachments if row.kind != "faq"}
        if old_docs != new_docs:
            output.append(BandoEventType.DOCUMENT_CHANGED)
        old_faq = {(row.attachment_id, row.content_hash) for row in previous.attachments if row.kind == "faq"}
        new_faq = {(row.attachment_id, row.content_hash) for row in current.attachments if row.kind == "faq"}
        if old_faq != new_faq:
            output.append(BandoEventType.FAQ_CHANGED)
        if previous.status is not BandoStatus.CLOSED and current.status is BandoStatus.CLOSED:
            output.append(BandoEventType.CLOSED)
        return tuple(output)


__all__ = ["BandiService", "BandoAttachment", "BandoEligibility", "BandoEventType", "BandoRequirements", "BandoStatus", "EligibilityOutcome", "NormalizedBando", "SourceAdapter", "TiremmEligibilityProfile"]
