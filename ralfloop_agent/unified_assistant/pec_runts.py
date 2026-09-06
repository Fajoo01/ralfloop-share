from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Protocol

from pydantic import Field, model_validator

from .contracts import StrictModel
from .event_router import EventOrigin, EventRouter, RoutedEvent, event_id
from .memory_service import MemoryDocument, MemoryEntity, MemoryEvent, MemoryService
from .platform import SourceRef
from .runtsuite_adapter import RuntsuitePracticeLink


class PecAttachment(StrictModel):
    attachment_id: str = Field(min_length=1, max_length=240)
    filename: str = Field(min_length=1, max_length=500)
    content_type: str | None = Field(default=None, max_length=160)
    size: int | None = Field(default=None, ge=0)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class PecMessage(StrictModel):
    native_id: str = Field(min_length=1, max_length=240)
    subject: str = Field(min_length=1, max_length=500)
    sender: str = Field(min_length=1, max_length=320)
    received_at: datetime
    observed_at: datetime
    body: str = Field(default="", max_length=100_000)
    unread: bool | None = None
    certified: bool | None = None
    attachments: tuple[PecAttachment, ...] = Field(default_factory=tuple, max_length=100)
    runts_reference: str | None = Field(default=None, max_length=240)
    source: SourceRef
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, **values: Any) -> "PecMessage":
        content = {key: value for key, value in values.items() if key != "content_hash"}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return cls(content_hash=digest, **values)


class RuntsAttachment(StrictModel):
    native_id: str = Field(min_length=1, max_length=240)
    name: str = Field(min_length=1, max_length=500)
    document_type: str | None = Field(default=None, max_length=160)
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source: SourceRef


class RuntsMessage(StrictModel):
    native_id: str = Field(min_length=1, max_length=240)
    practice_id: str | None = Field(default=None, max_length=240)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(default="", max_length=100_000)
    published_at: datetime | None = None
    observed_at: datetime
    action_required: bool = False
    attachments: tuple[RuntsAttachment, ...] = Field(default_factory=tuple, max_length=100)
    source: SourceRef
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, **values: Any) -> "RuntsMessage":
        content = {key: value for key, value in values.items() if key != "content_hash"}
        digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        return cls(content_hash=digest, **values)


class RuntsPractice(StrictModel):
    native_id: str = Field(min_length=1, max_length=240)
    status_raw: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=500)
    updated_at: datetime
    observed_at: datetime
    action_required: bool = False
    source: SourceRef
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class AuthorityStatus(StrEnum):
    VERIFIED_RUNTS = "VERIFIED_RUNTS"
    NOT_RUNTS_NOTIFICATION = "NOT_RUNTS_NOTIFICATION"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    NOT_FOUND = "NOT_FOUND"


class PecRuntsAuthorityResult(StrictModel):
    status: AuthorityStatus
    pec_message_id: str
    runts_reference: str | None = None
    runts_message: RuntsMessage | None = None
    sources: tuple[SourceRef, ...] = ()
    reason: str

    @model_validator(mode="after")
    def authoritative_shape(self):
        if self.status is AuthorityStatus.VERIFIED_RUNTS and self.runts_message is None:
            raise ValueError("runts_authoritative_message_required")
        return self


class RuntsActionProposal(StrictModel):
    proposal_id: str = Field(pattern=r"^proposal\.runts\.[a-f0-9]{24}$")
    practice_id: str
    action: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,95}$")
    reason: str = Field(min_length=1, max_length=1000)
    sources: tuple[SourceRef, ...] = Field(min_length=1, max_length=16)
    requires_approval: bool = True
    executable: bool = False
    created_at: datetime


class PecReadProvider(Protocol):
    def list_messages(self, *, limit: int) -> tuple[PecMessage, ...]: ...
    def get_message(self, native_id: str) -> PecMessage: ...


class RuntsReadProvider(Protocol):
    def list_messages(self, *, limit: int) -> tuple[RuntsMessage, ...]: ...
    def get_message(self, native_id: str) -> RuntsMessage: ...
    def list_practices(self, *, limit: int) -> tuple[RuntsPractice, ...]: ...
    def get_practice(self, native_id: str) -> RuntsPractice: ...


class RuntsuitePracticeProvider(Protocol):
    def find_runts_practice(self, runts_practice_id: str) -> RuntsuitePracticeLink: ...


class RuntsAuthRequired(RuntimeError):
    pass


class RuntsAuthBoundaryProvider:
    """Fail-closed provider used until an authenticated RUNTS contract is observed."""

    def list_messages(self, *, limit: int) -> tuple[RuntsMessage, ...]:
        raise RuntsAuthRequired("spid_cie_required")

    def get_message(self, native_id: str) -> RuntsMessage:
        raise RuntsAuthRequired("spid_cie_required")

    def list_practices(self, *, limit: int) -> tuple[RuntsPractice, ...]:
        raise RuntsAuthRequired("spid_cie_required")

    def get_practice(self, native_id: str) -> RuntsPractice:
        raise RuntsAuthRequired("spid_cie_required")


class PecRuntsService:
    def __init__(self, memory: MemoryService, pec: PecReadProvider, runts: RuntsReadProvider, *, runtsuite: RuntsuitePracticeProvider | None = None, router: EventRouter | None = None) -> None:
        self.memory, self.pec, self.runts, self.runtsuite, self.router = memory, pec, runts, runtsuite, router

    def discover_pec(self, *, limit: int = 100) -> tuple[PecMessage, ...]:
        rows = self.pec.list_messages(limit=_limit(limit))
        for row in rows:
            self._put_pec(row)
        return rows

    def get_pec(self, native_id: str) -> PecMessage:
        row = self.pec.get_message(native_id)
        if row.native_id != native_id:
            raise RuntimeError("pec_identity_mismatch")
        self._put_pec(row)
        return row

    def find_runts_notifications(self, *, limit: int = 100) -> tuple[PecMessage, ...]:
        return tuple(row for row in self.discover_pec(limit=limit) if row.runts_reference)

    def find_pec_by_runts_reference(self, runts_reference: str, *, limit: int = 100) -> tuple[PecMessage, ...]:
        identity = runts_reference.strip()
        if not identity or len(identity) > 240:
            raise ValueError("runts_reference_invalid")
        search = getattr(self.pec, "find_by_runts_reference", None)
        if search is not None:
            rows = search(identity, limit=_limit(limit))
            for row in rows:
                self._put_pec(row)
            return rows
        return tuple(row for row in self.discover_pec(limit=limit) if row.runts_reference == identity)

    def sync_runts(self, *, limit: int = 100) -> tuple[RuntsMessage | RuntsPractice, ...]:
        try:
            rows: tuple[RuntsMessage | RuntsPractice, ...] = (*self.runts.list_messages(limit=_limit(limit)), *self.runts.list_practices(limit=_limit(limit)))
        except RuntsAuthRequired:
            self._auth_required("runts.sync")
            raise
        for row in rows:
            self._put_runts(row)
        return rows

    def authoritative_for_pec(self, pec_message_id: str) -> PecRuntsAuthorityResult:
        pec = self.get_pec(pec_message_id)
        if not pec.runts_reference:
            return PecRuntsAuthorityResult(status=AuthorityStatus.NOT_RUNTS_NOTIFICATION, pec_message_id=pec.native_id, sources=(pec.source,), reason="pec_has_no_exact_runts_reference")
        try:
            message = self.runts.get_message(pec.runts_reference)
        except RuntsAuthRequired:
            self._auth_required(pec.runts_reference, pec.source)
            return PecRuntsAuthorityResult(status=AuthorityStatus.AUTH_REQUIRED, pec_message_id=pec.native_id, runts_reference=pec.runts_reference, sources=(pec.source,), reason="manual_spid_cie_authentication_required")
        except Exception:
            return PecRuntsAuthorityResult(status=AuthorityStatus.SOURCE_UNAVAILABLE, pec_message_id=pec.native_id, runts_reference=pec.runts_reference, sources=(pec.source,), reason="runts_source_unavailable")
        if message.native_id != pec.runts_reference:
            raise RuntimeError("runts_identity_mismatch")
        self._put_runts(message)
        return PecRuntsAuthorityResult(status=AuthorityStatus.VERIFIED_RUNTS, pec_message_id=pec.native_id, runts_reference=message.native_id, runts_message=message, sources=(pec.source, message.source), reason="administrative_content_from_authoritative_runts")

    def prepare_action(self, practice_id: str, action: str, reason: str, sources: tuple[SourceRef, ...], *, now: datetime | None = None) -> RuntsActionProposal:
        canonical = f"{practice_id}|{action}|{reason}"
        proposal = RuntsActionProposal(proposal_id="proposal.runts." + hashlib.sha256(canonical.encode()).hexdigest()[:24], practice_id=practice_id, action=action, reason=reason, sources=sources, created_at=now or datetime.now(timezone.utc))
        self.memory.put_entity(MemoryEntity.build(entity_id=proposal.proposal_id, domain="runts", entity_type="RUNTS_ACTION_PROPOSAL", status="WAITING_APPROVAL", updated_at=proposal.created_at, data=proposal.model_dump(mode="json"), provenance=sources))
        return proposal

    def correlate_runtsuite(self, practice_id: str) -> RuntsuitePracticeLink:
        if self.runtsuite is None:
            raise RuntimeError("runtsuite_source_unavailable")
        practice = self.runts.get_practice(practice_id)
        if practice.native_id != practice_id:
            raise RuntimeError("runts_identity_mismatch")
        self._put_runts(practice)
        return self.runtsuite.find_runts_practice(practice_id)

    def _put_pec(self, row: PecMessage) -> None:
        entity_id = "pec." + hashlib.sha256(row.native_id.encode()).hexdigest()[:32]
        fresh = self.memory.put_entity(MemoryEntity.build(entity_id=entity_id, domain="pec", entity_type="PEC_MESSAGE", status="UNREAD" if row.unread else "SEEN", updated_at=row.observed_at, data=row.model_dump(mode="json"), provenance=(row.source,)))
        if row.body:
            self.memory.put_document(MemoryDocument.build(document_id="document." + entity_id, title=row.subject, body=row.body, source=row.source))
        if fresh:
            self._emit("PEC_MESSAGE_DISCOVERED", row.native_id, row.observed_at, (entity_id,), {"runts_reference": row.runts_reference}, row.source)
            if row.runts_reference:
                self._emit("PEC_RUNTS_NOTIFICATION", row.native_id, row.observed_at, (entity_id,), {"runts_reference": row.runts_reference}, row.source)

    def _put_runts(self, row: RuntsMessage | RuntsPractice) -> None:
        kind = "message" if isinstance(row, RuntsMessage) else "practice"
        entity_id = f"runts.{kind}." + hashlib.sha256(row.native_id.encode()).hexdigest()[:24]
        event = "RUNTS_MESSAGE_DISCOVERED" if kind == "message" else "RUNTS_PRACTICE_UPDATED"
        fresh = self.memory.put_entity(MemoryEntity.build(entity_id=entity_id, domain="runts", entity_type=f"RUNTS_{kind.upper()}", status="ACTION_REQUIRED" if row.action_required else "CURRENT", updated_at=row.observed_at, data=row.model_dump(mode="json"), provenance=(row.source,)))
        if fresh:
            self._emit(event, row.native_id, row.observed_at, (entity_id,), {}, row.source)
            if row.action_required:
                self._emit("RUNTS_ACTION_REQUIRED", row.native_id, row.observed_at, (entity_id,), {}, row.source)
        if isinstance(row, RuntsMessage):
            if row.body:
                self.memory.put_document(MemoryDocument.build(document_id="document." + entity_id, title=row.subject, body=row.body, source=row.source))
            for attachment in row.attachments:
                self._emit("RUNTS_ATTACHMENT_DISCOVERED", attachment.native_id, row.observed_at, (entity_id,), {"message_id": row.native_id}, attachment.source)

    def _auth_required(self, source_id: str, source: SourceRef | None = None) -> None:
        ref = source or SourceRef(system="runts", native_id=source_id, locator="https://servizi.lavoro.gov.it/", observed_at=datetime.now(timezone.utc).isoformat())
        refs = (("pec." + hashlib.sha256(ref.native_id.encode()).hexdigest()[:32]),) if ref.system == "pec" else ()
        self._emit("RUNTS_AUTH_REQUIRED", source_id, datetime.now(timezone.utc), refs, {"manual_action": "complete SPID/CIE authentication in existing RUNTS browser tab"}, ref)

    def _emit(self, kind: str, source_id: str, when: datetime, refs: tuple[str, ...], payload: dict[str, Any], source: SourceRef) -> None:
        payload = {"event_type": kind, **payload}
        identity = event_id(source.system, source_id, kind, payload)
        routed = RoutedEvent(event_id=identity, event_type=kind, origin=EventOrigin.API_WATCHER, source=source.system, source_id=source_id, occurred_at=when, observed_at=when, entity_refs=refs, payload=payload, provenance=(source,))
        if self.router:
            self.router.route(routed)
        else:
            self.memory.append_event(routed.memory_event())


def _limit(value: int) -> int:
    if not 1 <= value <= 100:
        raise ValueError("pec_runts_limit_invalid")
    return value


__all__ = ["AuthorityStatus", "PecAttachment", "PecMessage", "PecReadProvider", "PecRuntsAuthorityResult", "PecRuntsService", "RuntsActionProposal", "RuntsAttachment", "RuntsAuthBoundaryProvider", "RuntsAuthRequired", "RuntsMessage", "RuntsPractice", "RuntsReadProvider", "RuntsuitePracticeProvider"]
