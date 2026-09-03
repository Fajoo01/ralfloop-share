from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping, Protocol

from pydantic import Field

from .contracts import StrictModel
from .event_router import EventOrigin, EventRouter, RoutedEvent, event_id
from .jellyfin_semantic import JellyfinMediaStream
from .memory_service import MemoryEntity, MemoryService
from .observability import OperationalMetrics
from .platform import SourceRef


class MediaTicketType(StrEnum):
    VIDEO_LOW_QUALITY = "VIDEO_LOW_QUALITY"
    VIDEO_CORRUPTED = "VIDEO_CORRUPTED"
    VIDEO_PLAYBACK_PROBLEM = "VIDEO_PLAYBACK_PROBLEM"
    AUDIO_LOW_QUALITY = "AUDIO_LOW_QUALITY"
    AUDIO_MISSING_ITALIAN = "AUDIO_MISSING_ITALIAN"
    AUDIO_WRONG_LANGUAGE = "AUDIO_WRONG_LANGUAGE"
    AUDIO_SYNC = "AUDIO_SYNC"
    SUBTITLE_MISSING = "SUBTITLE_MISSING"
    SUBTITLE_WRONG_LANGUAGE = "SUBTITLE_WRONG_LANGUAGE"
    SUBTITLE_SYNC = "SUBTITLE_SYNC"
    SUBTITLE_PLAYBACK_PROBLEM = "SUBTITLE_PLAYBACK_PROBLEM"
    WRONG_VERSION = "WRONG_VERSION"
    WRONG_METADATA = "WRONG_METADATA"
    OTHER = "OTHER"


class MediaTicketState(StrEnum):
    OPEN = "OPEN"
    TRIAGED = "TRIAGED"
    DIAGNOSED = "DIAGNOSED"
    FIX_PROPOSED = "FIX_PROPOSED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    FIXING = "FIXING"
    VERIFYING = "VERIFYING"
    RESOLVED = "RESOLVED"
    BLOCKED = "BLOCKED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    DUPLICATE = "DUPLICATE"
    WONT_FIX = "WONT_FIX"


class MediaEvidence(StrictModel):
    kind: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    summary: str = Field(min_length=1, max_length=1000)
    observed_at: datetime
    source: SourceRef
    data: dict[str, Any] = Field(default_factory=dict)


class MediaTicket(StrictModel):
    ticket_id: str = Field(pattern=r"^media\.[a-f0-9]{32}$")
    ticket_type: MediaTicketType
    state: MediaTicketState = MediaTicketState.OPEN
    jellyfin_item_id: str = Field(min_length=1, max_length=240)
    title: str | None = Field(default=None, max_length=500)
    series: str | None = Field(default=None, max_length=500)
    season: str | None = Field(default=None, max_length=240)
    episode: str | None = Field(default=None, max_length=240)
    user_report: str = Field(min_length=1, max_length=2000)
    playback_timestamp: int | None = Field(default=None, ge=0)
    client: str | None = Field(default=None, max_length=120)
    play_method: str | None = Field(default=None, max_length=80)
    selected_audio_stream: int | None = Field(default=None, ge=0)
    selected_subtitle_stream: int | None = Field(default=None, ge=0)
    reported_at: datetime
    updated_at: datetime
    evidence: tuple[MediaEvidence, ...] = Field(default_factory=tuple, max_length=100)
    diagnosis: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    duplicate_of: str | None = Field(default=None, pattern=r"^media\.[a-f0-9]{32}$")


class MediaFixProposal(StrictModel):
    proposal_id: str = Field(pattern=r"^proposal\.media\.[a-f0-9]{24}$")
    ticket_id: str
    action: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,95}$")
    reason: str = Field(min_length=1, max_length=1000)
    reversible: bool
    requires_approval: bool = True
    executable: bool = False
    created_at: datetime


class MediaScanResult(StrictModel):
    item_id: str
    findings: tuple[MediaTicketType, ...]
    streams: tuple[JellyfinMediaStream, ...]
    source: SourceRef


class JellyfinMediaReader(Protocol):
    def get_media_streams(self, item_id: str, *, user_id: str) -> tuple[JellyfinMediaStream, ...]: ...


OPEN_STATES = frozenset({
    MediaTicketState.OPEN, MediaTicketState.TRIAGED, MediaTicketState.DIAGNOSED,
    MediaTicketState.FIX_PROPOSED, MediaTicketState.WAITING_APPROVAL,
    MediaTicketState.FIXING, MediaTicketState.VERIFYING, MediaTicketState.BLOCKED,
    MediaTicketState.SOURCE_UNAVAILABLE,
})


class MediaQualityService:
    def __init__(self, memory: MemoryService, *, router: EventRouter | None = None, metrics: OperationalMetrics | None = None) -> None:
        self.memory = memory
        self.router = router
        self.metrics = metrics or OperationalMetrics()

    def create(self, *, ticket_type: MediaTicketType, jellyfin_item_id: str, user_report: str,
               source: SourceRef, reported_at: datetime | None = None, **context: Any) -> MediaTicket:
        stamp = reported_at or datetime.now(timezone.utc)
        report = _minimize_report(user_report)
        canonical = json.dumps({"type": ticket_type, "item": jellyfin_item_id}, sort_keys=True, separators=(",", ":"))
        identity = "media." + hashlib.sha256(canonical.encode()).hexdigest()[:32]
        existing = self.get(identity)
        if existing:
            self.metrics.increment("media_ticket_duplicates")
            return existing
        allowed = {"title", "series", "season", "episode", "playback_timestamp", "client", "play_method", "selected_audio_stream", "selected_subtitle_stream"}
        if set(context) - allowed:
            raise ValueError("media_context_field_forbidden")
        ticket = MediaTicket(ticket_id=identity, ticket_type=ticket_type, jellyfin_item_id=jellyfin_item_id,
                             user_report=report, reported_at=stamp, updated_at=stamp, **context)
        self._put(ticket, source)
        self.metrics.increment("media_tickets_open")
        self._emit(ticket, "MEDIA_TICKET_CREATED", source)
        return ticket

    def get(self, ticket_id: str) -> MediaTicket | None:
        row = self.memory.get_entity(ticket_id)
        return MediaTicket.model_validate(row.data) if row and row.domain == "media_quality" and row.entity_type == "MEDIA_TICKET" else None

    def list_open(self, *, limit: int = 100) -> tuple[MediaTicket, ...]:
        rows = self.memory.list_entities(domain="media_quality", limit=limit)
        return tuple(MediaTicket.model_validate(row.data) for row in rows if row.entity_type == "MEDIA_TICKET" and row.status in OPEN_STATES)

    def add_evidence(self, ticket_id: str, evidence: MediaEvidence) -> MediaTicket:
        ticket = self._require(ticket_id)
        updated = ticket.model_copy(update={"evidence": ticket.evidence + (evidence,), "updated_at": evidence.observed_at})
        self._put(updated, evidence.source)
        return updated

    def diagnose(self, ticket_id: str, findings: tuple[MediaTicketType, ...], source: SourceRef, *, now: datetime | None = None) -> MediaTicket:
        ticket = self._require(ticket_id)
        stamp = now or datetime.now(timezone.utc)
        labels = tuple(dict.fromkeys(row.value for row in findings))
        state = MediaTicketState.DIAGNOSED if labels else MediaTicketState.TRIAGED
        updated = ticket.model_copy(update={"diagnosis": labels, "state": state, "updated_at": stamp})
        self._put(updated, source)
        self.metrics.increment("media_diagnosed")
        return updated

    def prepare_fix(self, ticket_id: str, *, action: str, reason: str, reversible: bool,
                    source: SourceRef, now: datetime | None = None) -> MediaFixProposal:
        ticket = self._require(ticket_id)
        if ticket.state not in {MediaTicketState.TRIAGED, MediaTicketState.DIAGNOSED, MediaTicketState.BLOCKED}:
            raise ValueError("media_fix_state_invalid")
        stamp = now or datetime.now(timezone.utc)
        canonical = f"{ticket_id}|{action}|{reason}"
        proposal = MediaFixProposal(proposal_id="proposal.media." + hashlib.sha256(canonical.encode()).hexdigest()[:24],
                                    ticket_id=ticket_id, action=action, reason=reason, reversible=reversible, created_at=stamp)
        self.memory.put_entity(MemoryEntity.build(entity_id=proposal.proposal_id, domain="media_quality", entity_type="MEDIA_FIX_PROPOSAL",
                                                  status="WAITING_APPROVAL", updated_at=stamp, data=proposal.model_dump(mode="json"), provenance=(source,)))
        updated = ticket.model_copy(update={"state": MediaTicketState.WAITING_APPROVAL, "updated_at": stamp})
        self._put(updated, source)
        self.metrics.increment("media_fix_proposed")
        self._emit(updated, "MEDIA_FIX_PROPOSED", source, {"proposal_id": proposal.proposal_id})
        return proposal

    def verify_fix(self, ticket_id: str, *, passed: bool, evidence: MediaEvidence) -> MediaTicket:
        ticket = self.add_evidence(ticket_id, evidence)
        state = MediaTicketState.RESOLVED if passed else MediaTicketState.DIAGNOSED
        updated = ticket.model_copy(update={"state": state, "updated_at": evidence.observed_at})
        self._put(updated, evidence.source)
        if passed:
            self.metrics.increment("media_resolved")
            self._emit(updated, "MEDIA_FIX_VERIFIED", evidence.source)
        return updated

    def close(self, ticket_id: str, *, outcome: MediaTicketState, source: SourceRef, now: datetime | None = None) -> MediaTicket:
        if outcome not in {MediaTicketState.RESOLVED, MediaTicketState.DUPLICATE, MediaTicketState.WONT_FIX}:
            raise ValueError("media_close_outcome_invalid")
        ticket = self._require(ticket_id)
        updated = ticket.model_copy(update={"state": outcome, "updated_at": now or datetime.now(timezone.utc)})
        self._put(updated, source)
        return updated

    def scan_item(self, item_id: str, *, user_id: str, reader: JellyfinMediaReader,
                  min_height: int = 720, min_audio_bitrate: int = 96_000) -> MediaScanResult:
        observed = datetime.now(timezone.utc)
        source = SourceRef(system="jellyfin", native_id=item_id, locator=f"GET /Items/{item_id}/PlaybackInfo", observed_at=observed.isoformat())
        try:
            streams = reader.get_media_streams(item_id, user_id=user_id)
        except Exception:
            self.metrics.increment("media_probe_failures")
            self._emit_scan(item_id, (), "MEDIA_PROBE_FAILED", source)
            raise RuntimeError("media_source_unavailable") from None
        findings: list[MediaTicketType] = []
        video = [row for row in streams if row.type.casefold() == "video"]
        audio = [row for row in streams if row.type.casefold() == "audio"]
        subtitles = [row for row in streams if row.type.casefold() in {"subtitle", "subtitles"}]
        italian = {"ita", "it", "italian", "italiano"}
        if video and all((row.height or 0) < min_height for row in video):
            findings.append(MediaTicketType.VIDEO_LOW_QUALITY)
        has_italian_audio = any((row.language or "").casefold() in italian for row in audio)
        mislabeled_italian = any(
            (row.language or "").casefold() not in italian
            and any(marker in (row.display_title or "").casefold() for marker in ("italian", "italiano", " ita"))
            for row in audio
        )
        if mislabeled_italian:
            findings.append(MediaTicketType.AUDIO_WRONG_LANGUAGE)
        elif audio and not has_italian_audio:
            findings.append(MediaTicketType.AUDIO_MISSING_ITALIAN)
        if audio and all(row.bitrate is not None and row.bitrate < min_audio_bitrate for row in audio):
            findings.append(MediaTicketType.AUDIO_LOW_QUALITY)
        if subtitles and not any((row.language or "").casefold() in italian for row in subtitles):
            findings.append(MediaTicketType.SUBTITLE_WRONG_LANGUAGE)
        if not subtitles:
            findings.append(MediaTicketType.SUBTITLE_MISSING)
        if findings:
            self.metrics.increment("media_auto_detected", len(findings))
            language = {MediaTicketType.AUDIO_MISSING_ITALIAN, MediaTicketType.SUBTITLE_MISSING, MediaTicketType.SUBTITLE_WRONG_LANGUAGE}
            event_type = "MEDIA_LANGUAGE_MISSING" if set(findings) & language else "MEDIA_LOW_QUALITY_DETECTED"
            self._emit_scan(item_id, tuple(findings), event_type, source)
        return MediaScanResult(item_id=item_id, findings=tuple(findings), streams=streams, source=source)

    def _require(self, ticket_id: str) -> MediaTicket:
        ticket = self.get(ticket_id)
        if not ticket:
            raise ValueError("media_ticket_not_found")
        return ticket

    def _put(self, ticket: MediaTicket, source: SourceRef) -> None:
        self.memory.put_entity(MemoryEntity.build(entity_id=ticket.ticket_id, domain="media_quality", entity_type="MEDIA_TICKET",
                                                  status=ticket.state, updated_at=ticket.updated_at,
                                                  data=ticket.model_dump(mode="json"), provenance=(source,)))

    def _emit(self, ticket: MediaTicket, event_type: str, source: SourceRef, extra: Mapping[str, Any] | None = None) -> None:
        if not self.router:
            return
        payload = {"ticket_type": ticket.ticket_type, "state": ticket.state, **dict(extra or {})}
        self.router.route(RoutedEvent(event_id=event_id("media_quality", ticket.ticket_id, event_type, payload), event_type=event_type,
                                      origin=EventOrigin.SERVICE, source="media_quality", source_id=ticket.ticket_id,
                                      occurred_at=ticket.updated_at, observed_at=ticket.updated_at,
                                      entity_refs=(ticket.ticket_id, ticket.jellyfin_item_id), payload=payload, provenance=(source,)))

    def _emit_scan(self, item_id: str, findings: tuple[MediaTicketType, ...], event_type: str, source: SourceRef) -> None:
        if not self.router:
            return
        payload = {"findings": [row.value for row in findings]}
        self.router.route(RoutedEvent(event_id=event_id("media_quality", item_id, event_type, payload), event_type=event_type,
                                      origin=EventOrigin.SERVICE, source="media_quality", source_id=item_id,
                                      occurred_at=datetime.fromisoformat(source.observed_at), observed_at=datetime.fromisoformat(source.observed_at),
                                      entity_refs=(item_id,), payload=payload, provenance=(source,)))


def _minimize_report(value: str) -> str:
    report = value.strip()
    report = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[email-redacted]", report, flags=re.IGNORECASE)
    report = re.sub(r"(?<!\w)(?:\+?39[ .-]?)?(?:\d[ .-]?){9,10}(?!\w)", "[phone-redacted]", report)
    return report


__all__ = ["JellyfinMediaReader", "MediaEvidence", "MediaFixProposal", "MediaQualityService", "MediaScanResult", "MediaTicket", "MediaTicketState", "MediaTicketType", "OPEN_STATES"]
