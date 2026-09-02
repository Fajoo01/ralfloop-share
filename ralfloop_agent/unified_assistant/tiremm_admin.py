from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import MemoryItem, MemoryNamespace, MemoryProvenance, MemoryType, PolicyClass


class AdminModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceKind(StrEnum):
    GMAIL = "gmail"
    PEC = "pec"
    DOCUMENT = "document"
    CALENDAR = "calendar"
    ARCI = "arci"
    MAILCHIMP = "mailchimp"


class PracticeStatus(StrEnum):
    OPEN = "open"
    WAITING = "waiting"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class PracticePriority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class PracticeKind(StrEnum):
    PROJECT = "project"
    MUNICIPAL = "municipal"
    GRANT_REPORTING = "grant_reporting"
    FAMILY_COMMUNICATION = "family_communication"
    EVENT = "event"
    ARCI_MEMBERSHIP = "arci_membership"


class SourceRecord(AdminModel):
    source_kind: SourceKind
    source_id: str = Field(min_length=1, max_length=240)
    observed_at: datetime
    source_timestamp: datetime | None = None
    title: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=20_000)
    location: str = Field(min_length=1, max_length=1000)
    metadata: dict[str, str] = Field(default_factory=dict)
    cursor: str | None = Field(default=None, max_length=500)

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def evidence_id(self) -> str:
        payload = f"{self.source_kind}\0{self.source_id}\0{self.content_sha256}"
        return "src." + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class EvidenceRef(AdminModel):
    evidence_id: str = Field(pattern=r"^src\.[0-9a-f]{24}$")
    source_kind: SourceKind
    source_id: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    location: str
    observed_at: datetime
    source_timestamp: datetime | None = None
    trust: Literal["official", "authenticated", "reported", "unknown"] = "unknown"
    freshness: Literal["current", "stale", "unknown"] = "unknown"


class SourcedFact(AdminModel):
    field: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=2000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    inferred: bool = False


class PracticeArtifact(AdminModel):
    artifact_id: str = Field(min_length=1, max_length=240)
    label: str = Field(min_length=1, max_length=500)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)


class Deadline(AdminModel):
    label: str = Field(min_length=1, max_length=240)
    due_at: datetime
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)


class NextAction(AdminModel):
    action_type: Literal["review", "draft", "collect", "contact", "submit", "pay", "wait"]
    description: str = Field(min_length=1, max_length=1000)
    requires_approval: bool
    blocked_by: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    due_at: datetime | None = None
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)


class PracticeConflict(AdminModel):
    practice_id: str
    field: str
    values: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    detected_at: datetime
    resolution_status: Literal["unresolved", "resolved"] = "unresolved"


class Practice(AdminModel):
    schema_version: Literal[1] = 1
    practice_id: str = Field(pattern=r"^practice\.[a-z0-9][a-z0-9_.-]{0,87}$")
    title: str = Field(min_length=1, max_length=500)
    kind: PracticeKind
    status: PracticeStatus
    priority: PracticePriority = PracticePriority.NORMAL
    opened_at: datetime
    responsible_party: str = Field(min_length=1, max_length=240)
    counterparties: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=128)
    deadlines: tuple[Deadline, ...] = Field(default_factory=tuple, max_length=32)
    blockers: tuple[SourcedFact, ...] = Field(default_factory=tuple, max_length=32)
    communications: tuple[PracticeArtifact, ...] = Field(default_factory=tuple, max_length=128)
    documents: tuple[PracticeArtifact, ...] = Field(default_factory=tuple, max_length=128)
    events: tuple[PracticeArtifact, ...] = Field(default_factory=tuple, max_length=128)
    facts: tuple[SourcedFact, ...] = Field(default_factory=tuple, max_length=128)
    conflicts: tuple[PracticeConflict, ...] = Field(default_factory=tuple, max_length=32)
    needs_verification: bool = False
    next_action: NextAction | None = None
    updated_at: datetime

    @model_validator(mode="after")
    def references_are_declared(self) -> "Practice":
        declared = set(self.evidence_ids)
        referenced = {
            evidence_id
            for deadline in self.deadlines
            for evidence_id in deadline.evidence_ids
        }
        if self.next_action:
            referenced.update(self.next_action.evidence_ids)
        if not referenced <= declared:
            raise ValueError("practice_undeclared_evidence")
        for row in (*self.blockers, *self.facts, *self.communications, *self.documents, *self.events):
            referenced.update(row.evidence_ids)
        for conflict in self.conflicts:
            referenced.update(conflict.evidence_ids)
        if self.status in {PracticeStatus.COMPLETED, PracticeStatus.CANCELLED, PracticeStatus.ARCHIVED} and self.next_action:
            raise ValueError("closed_practice_cannot_have_next_action")
        if self.needs_verification != any(
            row.resolution_status == "unresolved" for row in self.conflicts
        ):
            raise ValueError("practice_verification_state_invalid")
        return self


class RetrievalHit(AdminModel):
    practice: Practice
    evidence: tuple[EvidenceRef, ...]
    conflicts: tuple[PracticeConflict, ...]


class TiremmAdminStore:
    """In-memory read-only projection. Source adapters remain authoritative."""

    def __init__(self, *, max_sources: int = 10_000, max_practices: int = 2_000) -> None:
        if max_sources < 1 or max_practices < 1:
            raise ValueError("tiremm_admin_quota_invalid")
        self.max_sources = max_sources
        self.max_practices = max_practices
        self._sources: dict[str, SourceRecord] = {}
        self._practices: dict[str, Practice] = {}
        self._conflicts: dict[str, tuple[PracticeConflict, ...]] = {}
        self.duplicates = 0

    def ingest_snapshot(self, records: Iterable[SourceRecord]) -> tuple[EvidenceRef, ...]:
        added: list[EvidenceRef] = []
        for record in records:
            existing = self._sources.get(record.evidence_id)
            if existing:
                if existing != record:
                    raise ValueError("evidence_hash_collision")
                self.duplicates += 1
                continue
            if len(self._sources) >= self.max_sources:
                raise ValueError("tiremm_admin_source_quota_exceeded")
            self._sources[record.evidence_id] = record
            added.append(self.evidence(record.evidence_id))
        return tuple(added)

    def evidence(self, evidence_id: str) -> EvidenceRef:
        try:
            source = self._sources[evidence_id]
        except KeyError as exc:
            raise KeyError("tiremm_admin_evidence_missing") from exc
        return EvidenceRef(
            evidence_id=evidence_id, source_kind=source.source_kind,
            source_id=source.source_id, content_sha256=source.content_sha256,
            location=source.location, observed_at=source.observed_at,
            source_timestamp=source.source_timestamp,
            trust=source.metadata.get("trust", "unknown"),
            freshness=source.metadata.get("freshness", "unknown"),
        )

    def source_record(self, evidence_id: str) -> SourceRecord:
        try:
            return self._sources[evidence_id]
        except KeyError as exc:
            raise KeyError("tiremm_admin_evidence_missing") from exc

    def project(self, practice: Practice) -> None:
        if practice.practice_id not in self._practices and len(self._practices) >= self.max_practices:
            raise ValueError("tiremm_admin_practice_quota_exceeded")
        missing = set(practice.evidence_ids) - self._sources.keys()
        if missing:
            raise ValueError("practice_evidence_missing")
        current = self._practices.get(practice.practice_id)
        if current and practice.updated_at < current.updated_at:
            raise ValueError("practice_projection_stale")
        conflicts = self._deadline_conflicts(practice)
        projected = Practice.model_validate({
            **practice.model_dump(mode="python"),
            "conflicts": conflicts,
            "needs_verification": bool(conflicts),
        })
        self._practices[practice.practice_id] = projected
        self._conflicts[practice.practice_id] = conflicts

    def transition(
        self, practice_id: str, new_status: PracticeStatus, *,
        evidence_ids: tuple[str, ...], updated_at: datetime,
    ) -> Practice:
        current = self._require_practice(practice_id)
        allowed = {
            PracticeStatus.OPEN: {PracticeStatus.WAITING, PracticeStatus.BLOCKED, PracticeStatus.COMPLETED, PracticeStatus.CANCELLED},
            PracticeStatus.WAITING: {PracticeStatus.OPEN, PracticeStatus.BLOCKED, PracticeStatus.COMPLETED, PracticeStatus.CANCELLED},
            PracticeStatus.BLOCKED: {PracticeStatus.OPEN, PracticeStatus.WAITING, PracticeStatus.COMPLETED, PracticeStatus.CANCELLED},
            PracticeStatus.COMPLETED: {PracticeStatus.ARCHIVED},
            PracticeStatus.CANCELLED: {PracticeStatus.ARCHIVED},
            PracticeStatus.ARCHIVED: set(),
        }
        if new_status not in allowed[current.status]:
            raise ValueError("practice_state_transition_invalid")
        if not evidence_ids or set(evidence_ids) - self._sources.keys():
            raise ValueError("practice_transition_evidence_missing")
        merged = tuple(dict.fromkeys((*current.evidence_ids, *evidence_ids)))
        next_action = (
            None if new_status in {PracticeStatus.COMPLETED, PracticeStatus.CANCELLED, PracticeStatus.ARCHIVED}
            else current.next_action
        )
        updated = current.model_copy(update={
            "status": new_status, "evidence_ids": merged,
            "next_action": next_action, "updated_at": updated_at,
        })
        self.project(updated)
        return updated

    def retrieve(self, query: str, *, limit: int = 20) -> tuple[RetrievalHit, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("tiremm_admin_limit_invalid")
        terms = {term.casefold() for term in re.findall(r"[\wÀ-ÿ]+", query) if len(term) > 1}
        ranked: list[tuple[int, str, Practice]] = []
        for practice in self._practices.values():
            haystack = " ".join((
                practice.practice_id, practice.title, practice.responsible_party,
                *practice.counterparties,
                *(deadline.label for deadline in practice.deadlines),
                practice.next_action.description if practice.next_action else "",
                *(row.label for row in (*practice.communications, *practice.documents, *practice.events)),
                *(row.value for row in (*practice.blockers, *practice.facts)),
            )).casefold()
            score = sum(term in haystack for term in terms)
            if score:
                ranked.append((score, practice.practice_id, practice))
        ranked.sort(key=lambda row: (-row[0], row[1]))
        return tuple(RetrievalHit(
            practice=practice,
            evidence=tuple(self.evidence(item) for item in practice.evidence_ids),
            conflicts=self._conflicts.get(practice.practice_id, ()),
        ) for _, _, practice in ranked[:limit])

    def list_open_practices(self) -> tuple[Practice, ...]:
        closed = {PracticeStatus.COMPLETED, PracticeStatus.CANCELLED, PracticeStatus.ARCHIVED}
        return self._sorted(row for row in self._practices.values() if row.status not in closed)

    def get_practice(self, practice_id: str) -> Practice | None:
        return self._practices.get(practice_id)

    def get_due_practices(self, *, now: datetime, within: timedelta | None = None) -> tuple[Practice, ...]:
        end = now + within if within is not None else None
        return self._sorted(row for row in self.list_open_practices() if any(
            deadline.due_at >= now and (end is None or deadline.due_at <= end)
            for deadline in row.deadlines
        ))

    def overdue(self, now: datetime) -> tuple[Practice, ...]:
        return self._sorted(row for row in self.list_open_practices() if any(
            deadline.due_at < now for deadline in row.deadlines
        ))

    def due_within(self, now: datetime, delta: timedelta) -> tuple[Practice, ...]:
        if delta.total_seconds() < 0:
            raise ValueError("deadline_delta_negative")
        return self.get_due_practices(now=now, within=delta)

    def blocked(self) -> tuple[Practice, ...]:
        return self._sorted(row for row in self._practices.values() if row.status is PracticeStatus.BLOCKED)

    def waiting(self) -> tuple[Practice, ...]:
        return self._sorted(row for row in self._practices.values() if row.status is PracticeStatus.WAITING)

    def no_next_action(self) -> tuple[Practice, ...]:
        return self._sorted(row for row in self.list_open_practices() if row.next_action is None)

    def stale_since(self, now: datetime, delta: timedelta) -> tuple[Practice, ...]:
        if delta.total_seconds() < 0:
            raise ValueError("stale_delta_negative")
        threshold = now - delta
        return self._sorted(row for row in self.list_open_practices() if row.updated_at < threshold)

    def get_blocked_practices(self) -> tuple[Practice, ...]:
        return self.blocked()

    def get_next_actions(self) -> tuple[tuple[str, NextAction], ...]:
        return tuple(
            (row.practice_id, row.next_action)
            for row in self.list_open_practices() if row.next_action is not None
        )

    def get_sources(self, practice_id: str) -> tuple[EvidenceRef, ...]:
        practice = self._require_practice(practice_id)
        return tuple(self.evidence(item) for item in practice.evidence_ids)

    def get_conflicts(self, practice_id: str) -> tuple[PracticeConflict, ...]:
        self._require_practice(practice_id)
        return self._conflicts.get(practice_id, ())

    def latest_verified_update(self, practice_id: str) -> EvidenceRef:
        sources = self.get_sources(practice_id)
        verified = [row for row in sources if row.trust in {"official", "authenticated"}]
        if not verified:
            raise ValueError("verified_source_missing")
        return max(verified, key=lambda row: (row.source_timestamp or row.observed_at, row.evidence_id))

    def validate_action_proposal(self, proposal: ActionProposal) -> ActionProposal:
        if set(proposal.evidence_ids) - self._sources.keys():
            raise ValueError("action_proposal_evidence_missing")
        return proposal

    def _require_practice(self, practice_id: str) -> Practice:
        try:
            return self._practices[practice_id]
        except KeyError as exc:
            raise KeyError("tiremm_admin_practice_missing") from exc

    @staticmethod
    def _sorted(rows: Iterable[Practice]) -> tuple[Practice, ...]:
        return tuple(sorted(rows, key=lambda row: (row.updated_at, row.practice_id), reverse=True))

    def memory_items(self) -> tuple[MemoryItem, ...]:
        result = []
        for practice in sorted(self._practices.values(), key=lambda row: row.practice_id):
            content = json.dumps(practice.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
            result.append(MemoryItem(
                id=practice.practice_id,
                namespace=MemoryNamespace.TIREMM,
                memory_type=MemoryType.DOCUMENT,
                subject=practice.title,
                slot="admin_practice",
                content=content[:4000],
                epistemic_kind="evidence",
                timestamp=practice.updated_at.isoformat(),
                provenance=MemoryProvenance.DOCUMENT,
                certainty="verified",
                source_refs=tuple(sorted(practice.evidence_ids))[:16],
            ))
        return tuple(result)

    def metrics(self) -> dict[str, int]:
        payload_bytes = sum(len(row.content.encode("utf-8")) for row in self._sources.values())
        return {
            "tiremm_admin_sources": len(self._sources),
            "tiremm_admin_practices": len(self._practices),
            "tiremm_admin_conflicts": sum(map(len, self._conflicts.values())),
            "tiremm_admin_duplicates": self.duplicates,
            "tiremm_admin_source_bytes": payload_bytes,
        }

    @staticmethod
    def _deadline_conflicts(practice: Practice) -> tuple[PracticeConflict, ...]:
        grouped: dict[str, list[Deadline]] = defaultdict(list)
        for deadline in practice.deadlines:
            grouped[deadline.label.casefold()].append(deadline)
        conflicts = []
        for label, deadlines in grouped.items():
            values = sorted({item.due_at.isoformat() for item in deadlines})
            if len(values) > 1:
                conflicts.append(PracticeConflict(
                    practice_id=practice.practice_id, field=f"deadline:{label}",
                    values=tuple(values),
                    evidence_ids=tuple(sorted({ref for item in deadlines for ref in item.evidence_ids})),
                    detected_at=practice.updated_at,
                ))
        return tuple(conflicts)


class TiremmAdminEvalCase(AdminModel):
    case_id: str
    query: str
    expected_practice_ids: tuple[str, ...]
    expected_conflict_count: int = 0
    expected_requires_approval: bool | None = None


class PracticeUpdateProposal(AdminModel):
    source: EvidenceRef
    practice_id: str | None
    changed_since_last_ingestion: bool
    normalized_record: SourceRecord
    persistence_allowed: bool = False


class TiremmIngestionPipeline:
    """READ happens outside; this layer only normalizes, reconciles, and proposes."""

    def __init__(self, store: TiremmAdminStore) -> None:
        self.store = store
        self._last_seen: dict[tuple[SourceKind, str], str] = {}
        self.cursors: dict[SourceKind, str] = {}

    def ingest(self, raw: dict[str, Any]) -> PracticeUpdateProposal:
        record = SourceRecord.model_validate(raw)
        identity = (record.source_kind, record.source_id)
        changed = self._last_seen.get(identity) != record.content_sha256
        added = self.store.ingest_snapshot((record,))
        self._last_seen[identity] = record.content_sha256
        if record.cursor:
            self.cursors[record.source_kind] = record.cursor
        evidence = added[0] if added else self.store.evidence(record.evidence_id)
        return PracticeUpdateProposal(
            source=evidence,
            practice_id=record.metadata.get("practice_id"),
            changed_since_last_ingestion=changed,
            normalized_record=record,
        )


class TiremmAdminSQLite:
    """Experimental local persistence; explicit calls only, never enabled by default."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_records (
                    evidence_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL, last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS practices (
                    practice_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL
                );
            """)
            current = self.connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if current and int(current[0]) != self.SCHEMA_VERSION:
                raise ValueError("tiremm_admin_schema_migration_required")
            self.connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )

    def persist_source(self, record: SourceRecord, *, last_seen: datetime) -> None:
        payload = record.model_dump_json()
        with self.connection:
            self.connection.execute("""
                INSERT INTO source_records(evidence_id,payload,content_sha256,last_seen)
                VALUES(?,?,?,?)
                ON CONFLICT(evidence_id) DO UPDATE SET last_seen=excluded.last_seen
            """, (record.evidence_id, payload, record.content_sha256, last_seen.isoformat()))

    def persist_practice(self, practice: Practice) -> None:
        with self.connection:
            self.connection.execute("""
                INSERT INTO practices(practice_id,schema_version,payload,updated_at)
                VALUES(?,?,?,?)
                ON CONFLICT(practice_id) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                WHERE excluded.updated_at >= practices.updated_at
            """, (
                practice.practice_id, practice.schema_version,
                practice.model_dump_json(), practice.updated_at.isoformat(),
            ))

    def load(self) -> TiremmAdminStore:
        store = TiremmAdminStore()
        sources = [
            SourceRecord.model_validate_json(row[0])
            for row in self.connection.execute("SELECT payload FROM source_records ORDER BY evidence_id")
        ]
        store.ingest_snapshot(sources)
        for row in self.connection.execute("SELECT payload FROM practices ORDER BY practice_id"):
            store.project(Practice.model_validate_json(row[0]))
        return store

    def close(self) -> None:
        self.connection.close()


class ActionProposal(AdminModel):
    action: Literal["draft_email", "draft_document", "propose_calendar_event", "propose_update"]
    target: str = Field(min_length=1, max_length=500)
    payload: dict[str, Any]
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=16)
    reason: str = Field(min_length=1, max_length=1000)
    requires_approval: Literal[True] = True


def reject_external_execution(_: ActionProposal) -> None:
    raise PermissionError("tiremm_admin_execute_disabled")


class TiremmAdminContext(AdminModel):
    query_type: Literal["open", "due", "blocked", "waiting", "next_actions", "practice", "unknown"]
    practices: tuple[dict[str, Any], ...]
    sources: tuple[EvidenceRef, ...]
    verified: bool
    validation_error: str | None = None


class TiremmAdminQueryAdapter:
    """Deterministic retrieval before an optional LLM presentation step."""

    def __init__(self, store: TiremmAdminStore) -> None:
        self.store = store

    def context(self, request: str, *, now: datetime) -> TiremmAdminContext:
        folded = request.casefold()
        words = set(re.findall(r"[\wÀ-ÿ]+", folded))
        if "blocc" in folded:
            query_type, rows = "blocked", self.store.blocked()
        elif "attesa" in folded:
            query_type, rows = "waiting", self.store.waiting()
        elif "scaden" in folded or "entro" in folded:
            query_type, rows = "due", (*self.store.overdue(now), *self.store.due_within(now, timedelta(days=30)))
        elif "prossima" in words or "azione" in words or "azioni" in words:
            query_type, rows = "next_actions", tuple(
                self.store.get_practice(identity) for identity, _ in self.store.get_next_actions()
            )
        elif "apert" in folded:
            query_type, rows = "open", self.store.list_open_practices()
        else:
            hits = self.store.retrieve(request)
            query_type, rows = ("practice", tuple(hit.practice for hit in hits)) if hits else ("unknown", ())
        unique = tuple({row.practice_id: row for row in rows if row is not None}.values())
        sources = tuple({source.evidence_id: source for row in unique for source in self.store.get_sources(row.practice_id)}.values())
        verified = bool(unique) and all(row.evidence_ids for row in unique) and bool(sources)
        return TiremmAdminContext(
            query_type=query_type,
            practices=tuple(row.model_dump(mode="json") for row in unique),
            sources=sources,
            verified=verified,
            validation_error=None if verified else "source_backed_information_missing",
        )


class TiremmAdminEvalResult(AdminModel):
    case_id: str
    passed: bool
    found_practice_ids: tuple[str, ...]
    conflict_count: int
    reason: str


def evaluate_admin(store: TiremmAdminStore, cases: Iterable[TiremmAdminEvalCase]) -> tuple[TiremmAdminEvalResult, ...]:
    results = []
    for case in cases:
        hits = store.retrieve(case.query)
        found = tuple(hit.practice.practice_id for hit in hits)
        conflicts = sum(len(hit.conflicts) for hit in hits)
        policy_ok = case.expected_requires_approval is None or (
            bool(hits) and hits[0].practice.next_action is not None
            and hits[0].practice.next_action.requires_approval is case.expected_requires_approval
        )
        passed = set(case.expected_practice_ids) <= set(found) and conflicts == case.expected_conflict_count and policy_ok
        results.append(TiremmAdminEvalResult(
            case_id=case.case_id, passed=passed, found_practice_ids=found,
            conflict_count=conflicts, reason="ok" if passed else "deterministic_expectation_failed",
        ))
    return tuple(results)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


__all__ = [
    "ActionProposal", "Deadline", "EvidenceRef", "NextAction", "Practice",
    "PracticeArtifact", "PracticeConflict", "PracticeKind", "PracticePriority",
    "PracticeStatus", "PracticeUpdateProposal", "RetrievalHit", "SourceKind",
    "SourceRecord", "SourcedFact", "TiremmAdminContext", "TiremmAdminQueryAdapter",
    "TiremmAdminSQLite", "TiremmIngestionPipeline",
    "TiremmAdminEvalCase", "TiremmAdminEvalResult", "TiremmAdminStore",
    "evaluate_admin", "reject_external_execution", "utc_now",
]
