from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests
from pydantic import Field

from .contracts import StrictModel
from .memory_service import MemoryDocument, MemoryEvent, MemoryService
from .platform import SourceRef
from .task_queue import BotTazziTaskQueue


class AgendaKind(StrEnum):
    TASK = "task"
    APPOINTMENT = "appointment"
    NOTIFICATION = "notification"
    INFORMATION = "information"


class AgendaSource(StrictModel):
    channel: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    sender: str = Field(min_length=1, max_length=500)
    native_id: str = Field(min_length=1, max_length=500)
    timestamp: datetime
    original_text: str = Field(min_length=1, max_length=50_000)


class AgendaCandidate(StrictModel):
    kind: AgendaKind
    title: str = Field(min_length=1, max_length=500)
    start_at: datetime | None = None
    end_at: datetime | None = None
    deadline_at: datetime | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    uncertain: bool = False
    needs_motor: bool = False
    semantic_key: str = Field(min_length=1, max_length=1000)


class CalendarEventRequest(StrictModel):
    title: str = Field(min_length=1, max_length=500)
    start_at: datetime
    end_at: datetime
    description: str = Field(default="", max_length=10_000)


class CalendarReceipt(StrictModel):
    provider: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=500)
    locator: str = Field(min_length=1, max_length=2000)


class AgendaResult(StrictModel):
    kind: AgendaKind
    dedup_key: str
    duplicate: bool
    outcome_id: str | None = None
    calendar_receipt: CalendarReceipt | None = None
    candidate: AgendaCandidate


class CalendarProvider(Protocol):
    def create_event(self, event: CalendarEventRequest) -> CalendarReceipt: ...


class AmbiguityResolver(Protocol):
    def resolve(self, source: AgendaSource, candidate: AgendaCandidate) -> AgendaCandidate: ...


class LocalSTT(Protocol):
    def transcribe(self, content: bytes, mime_type: str) -> str: ...


_WEEKDAYS = {
    "lunedi": 0, "lunedì": 0, "martedi": 1, "martedì": 1,
    "mercoledi": 2, "mercoledì": 2, "giovedi": 3, "giovedì": 3,
    "venerdi": 4, "venerdì": 4, "sabato": 5, "domenica": 6,
}
_UNCERTAIN_RE = re.compile(r"\b(forse|magari|probabilmente|eventualmente|potremmo|potrei|se riesco|da confermare)\b", re.I)
_TIME_RE = re.compile(r"\b(?:alle|ore)\s*(\d{1,2})(?::([0-5]\d))?\b", re.I)
_CLAUSE_SPLIT_RE = re.compile(r"(?:[.!?;]+(?:\s+|$)|\n+)")


def _compact(text: str) -> str:
    return " ".join(text.split())


def _normal(text: str) -> str:
    value = text.casefold().replace("’", "'")
    value = re.sub(r"[^\wàèéìòù']+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _agenda_timezone() -> ZoneInfo:
    configured = os.getenv("BOTTAZZI_AGENDA_TIMEZONE", "Europe/Rome").strip() or "Europe/Rome"
    return ZoneInfo(configured)


def _day(text: str, reference: datetime) -> datetime | None:
    tz = _agenda_timezone()
    ref = reference.replace(tzinfo=tz) if reference.tzinfo is None else reference.astimezone(tz)
    low = _normal(text)
    if re.search(r"\boggi\b", low):
        target = ref.date()
    elif re.search(r"\bdomani\b", low):
        target = (ref + timedelta(days=1)).date()
    else:
        target = None
        for name, weekday in _WEEKDAYS.items():
            if re.search(rf"\b{re.escape(name)}\b", low):
                delta = (weekday - ref.weekday()) % 7
                target = (ref + timedelta(days=delta)).date()
                break
        if target is None:
            return None
    return datetime.combine(target, time(0, 0), tzinfo=tz)


def _when(text: str, reference: datetime, *, end_of_day: bool = False) -> datetime | None:
    day = _day(text, reference)
    if day is None:
        return None
    match = _TIME_RE.search(text)
    if match:
        return day.replace(hour=int(match.group(1)), minute=int(match.group(2) or 0))
    if end_of_day:
        return day.replace(hour=23, minute=59)
    return None


def _semantic_core(text: str) -> str:
    value = _normal(text)
    value = re.sub(r"\b(oggi|domani|lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|giovedi|giovedì|venerdi|venerdì|sabato|domenica)\b", " ", value)
    value = re.sub(r"\b(alle|ore)\s*\d{1,2}(?::\d{2})?\b", " ", value)
    return " ".join(value.split())


class AgendaExtractor:
    """Deterministic fast lane. Uncertain commitments never authorize a side effect."""

    def extract_many(self, source: AgendaSource) -> tuple[AgendaCandidate, ...]:
        clauses = tuple(
            clause for clause in (_compact(part) for part in _CLAUSE_SPLIT_RE.split(source.original_text))
            if clause
        )
        if len(clauses) <= 1:
            return (self.extract(source),)
        actionable: list[AgendaCandidate] = []
        for clause in clauses:
            candidate = self.extract(source.model_copy(update={"original_text": clause}))
            if candidate.kind is not AgendaKind.INFORMATION or candidate.needs_motor:
                actionable.append(candidate)
        return tuple(actionable) if actionable else (self.extract(source),)

    def extract(self, source: AgendaSource) -> AgendaCandidate:
        text = _compact(source.original_text)
        uncertain = bool(_UNCERTAIN_RE.search(text))
        reminder = re.search(r"\bricordami\s+di\s+(.+)$", text, re.I)
        if reminder:
            raw_action = reminder.group(1).strip(" .")
            action = re.split(
                r"\s+(?:oggi|domani|lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|giovedi|giovedì|venerdi|venerdì|sabato|domenica)(?:\s+alle\s+\d{1,2}(?::\d{2})?)?\b",
                raw_action, maxsplit=1, flags=re.I,
            )[0].strip(" ,.-") or raw_action
            deadline = _when(text, source.timestamp, end_of_day=True)
            return AgendaCandidate(
                kind=AgendaKind.TASK, title=action, deadline_at=deadline,
                confidence=0.99 if not uncertain else 0.65, uncertain=uncertain,
                needs_motor=uncertain,
                semantic_key=f"task|{_normal(action)}|{deadline.isoformat() if deadline else ''}",
            )
        if re.search(r"\b(avvisami|notificami)\b", text, re.I):
            when = _when(text, source.timestamp, end_of_day=True)
            return AgendaCandidate(
                kind=AgendaKind.NOTIFICATION, title=text, deadline_at=when,
                confidence=0.95 if not uncertain else 0.6, uncertain=uncertain,
                needs_motor=uncertain,
                semantic_key=f"notification|{_semantic_core(text)}|{when.isoformat() if when else ''}",
            )
        meeting_signal = bool(re.search(r"\b(ci vediamo|appuntamento|incontro)\b", text, re.I))
        start = _when(text, source.timestamp)
        if meeting_signal and start is not None:
            core = _semantic_core(text)
            if uncertain:
                return AgendaCandidate(
                    kind=AgendaKind.INFORMATION, title=text, confidence=0.55,
                    uncertain=True, needs_motor=True,
                    semantic_key=f"information|{core}|{start.isoformat()}",
                )
            return AgendaCandidate(
                kind=AgendaKind.APPOINTMENT, title=f"Incontro con {source.sender}",
                start_at=start, end_at=start + timedelta(hours=1), confidence=0.99,
                semantic_key=f"appointment|{core}|{start.isoformat()}",
            )
        return AgendaCandidate(
            kind=AgendaKind.INFORMATION, title=text[:500], confidence=0.98,
            uncertain=uncertain, needs_motor=uncertain,
            semantic_key=f"information|{_normal(text)}",
        )


class AgendaStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                "CREATE TABLE IF NOT EXISTS items(dedup_key TEXT PRIMARY KEY,kind TEXT NOT NULL,outcome_id TEXT,payload_json TEXT NOT NULL,created_at TEXT NOT NULL);"
                "CREATE TABLE IF NOT EXISTS sources(source_key TEXT PRIMARY KEY,dedup_key TEXT NOT NULL,channel TEXT NOT NULL,sender TEXT NOT NULL,native_id TEXT NOT NULL,source_timestamp TEXT NOT NULL,original_text TEXT NOT NULL,FOREIGN KEY(dedup_key) REFERENCES items(dedup_key));"
                "CREATE INDEX IF NOT EXISTS sources_dedup ON sources(dedup_key);"
            )

    @classmethod
    def from_env(cls) -> "AgendaStore":
        configured = os.getenv("BOTTAZZI_AGENDA_DB", "").strip()
        if configured:
            return cls(configured)
        root = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))).expanduser()
        return cls(root / "bottazzi" / "runtime-production" / "agenda.sqlite3")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def get(self, dedup_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM items WHERE dedup_key=?", (dedup_key,)).fetchone()
        return dict(row) if row else None

    def put_item(self, dedup_key: str, kind: AgendaKind, outcome_id: str | None, payload: dict) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO items(dedup_key,kind,outcome_id,payload_json,created_at) VALUES(?,?,?,?,?)",
                (dedup_key, kind.value, outcome_id, json.dumps(payload, ensure_ascii=False, sort_keys=True), datetime.now(timezone.utc).isoformat()),
            )

    def link_source(self, dedup_key: str, source: AgendaSource) -> None:
        source_key = hashlib.sha256(f"{source.channel}\0{source.native_id}\0{dedup_key}".encode()).hexdigest()
        with self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO sources(source_key,dedup_key,channel,sender,native_id,source_timestamp,original_text) VALUES(?,?,?,?,?,?,?)",
                (source_key, dedup_key, source.channel, source.sender, source.native_id, source.timestamp.isoformat(), source.original_text),
            )

    def sources(self, dedup_key: str) -> tuple[dict, ...]:
        with self._connect() as conn:
            rows = conn.execute("SELECT channel,sender,native_id,source_timestamp,original_text FROM sources WHERE dedup_key=? ORDER BY source_timestamp,native_id", (dedup_key,)).fetchall()
        return tuple(dict(row) for row in rows)


class LocalIcsCalendarProvider:
    """Persistent self-hosted fallback and deterministic test provider."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def create_event(self, event: CalendarEventRequest) -> CalendarReceipt:
        event_id = f"agenda-{uuid4().hex}"
        path = self.root / f"{event_id}.ics"
        path.write_text(_ics(event_id, event), encoding="utf-8")
        return CalendarReceipt(provider="local-ics", event_id=event_id, locator=str(path))


class CalDavCalendarProvider:
    """Small RFC4791-compatible writer; canonical target is self-hosted Nextcloud."""

    def __init__(self, collection_url: str, username: str, password: str, *, timeout: float = 20.0, verify_tls: bool = True, session: requests.Session | None = None) -> None:
        self.collection_url = collection_url.rstrip("/") + "/"
        self.username = username
        self.password = password
        self.timeout = timeout
        self.verify_tls = verify_tls
        self.session = session or requests.Session()

    @classmethod
    def from_env(cls) -> "CalDavCalendarProvider":
        url = os.getenv("BOTTAZZI_CALDAV_URL", "").strip()
        username = os.getenv("BOTTAZZI_CALDAV_USERNAME", "").strip()
        password = os.getenv("BOTTAZZI_CALDAV_PASSWORD", "")
        if not (url and username and password):
            raise RuntimeError("caldav_configuration_missing")
        verify = os.getenv("BOTTAZZI_CALDAV_VERIFY_TLS", "1").casefold() not in {"0", "false", "no", "off"}
        return cls(url, username, password, verify_tls=verify)

    def create_event(self, event: CalendarEventRequest) -> CalendarReceipt:
        event_id = f"agenda-{uuid4().hex}"
        locator = f"{self.collection_url}{event_id}.ics"
        response = self.session.put(
            locator, data=_ics(event_id, event).encode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            auth=(self.username, self.password), timeout=self.timeout, verify=self.verify_tls,
        )
        try:
            if response.status_code not in {201, 204}:
                raise RuntimeError(f"caldav_create_failed:{response.status_code}")
        finally:
            response.close()
        return CalendarReceipt(provider="caldav", event_id=event_id, locator=locator)


def calendar_provider_from_env() -> CalendarProvider:
    if os.getenv("BOTTAZZI_CALDAV_URL", "").strip():
        return CalDavCalendarProvider.from_env()
    configured = os.getenv("BOTTAZZI_AGENDA_CALENDAR_DIR", "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        data_root = Path(os.getenv("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))).expanduser()
        root = data_root / "bottazzi" / "runtime-production" / "calendar"
    return LocalIcsCalendarProvider(root)


def _ics_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _ics(event_id: str, event: CalendarEventRequest) -> str:
    start = event.start_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    end = event.end_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "\r\n".join((
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Tiremm Innanz//Bot-tazzi Agenda//IT",
        "BEGIN:VEVENT", f"UID:{event_id}@bottazzi.local", f"DTSTAMP:{stamp}", f"DTSTART:{start}", f"DTEND:{end}",
        f"SUMMARY:{_ics_escape(event.title)}", f"DESCRIPTION:{_ics_escape(event.description)}", "END:VEVENT", "END:VCALENDAR", "",
    ))


@dataclass
class AgendaPipeline:
    store: AgendaStore
    queue: BotTazziTaskQueue
    memory: MemoryService
    calendar: CalendarProvider
    extractor: AgendaExtractor = AgendaExtractor()
    ambiguity_resolver: AmbiguityResolver | None = None

    def process(self, source: AgendaSource) -> AgendaResult:
        return self._process_candidate(source, self.extractor.extract(source))

    def process_all(self, source: AgendaSource) -> tuple[AgendaResult, ...]:
        return tuple(
            self._process_candidate(source, candidate)
            for candidate in self.extractor.extract_many(source)
        )

    def _process_candidate(self, source: AgendaSource, candidate: AgendaCandidate) -> AgendaResult:
        if candidate.needs_motor and self.ambiguity_resolver is not None:
            resolved = self.ambiguity_resolver.resolve(source, candidate)
            if not candidate.uncertain or resolved.kind is not AgendaKind.APPOINTMENT:
                candidate = resolved
        dedup_key = hashlib.sha256(candidate.semantic_key.encode()).hexdigest()
        existing = self.store.get(dedup_key)
        if existing is not None:
            self.store.link_source(dedup_key, source)
            self._remember_source(source, candidate, dedup_key, existing.get("outcome_id"))
            payload = json.loads(existing["payload_json"])
            receipt = CalendarReceipt.model_validate(payload["calendar_receipt"]) if payload.get("calendar_receipt") else None
            return AgendaResult(kind=AgendaKind(existing["kind"]), dedup_key=dedup_key, duplicate=True, outcome_id=existing.get("outcome_id"), calendar_receipt=receipt, candidate=candidate)
        outcome_id: str | None = None
        receipt: CalendarReceipt | None = None
        if candidate.kind in {AgendaKind.TASK, AgendaKind.NOTIFICATION}:
            description = self._provenance_description(source)
            if candidate.kind is AgendaKind.NOTIFICATION:
                description = "Bot-tazzi Agenda notification\n" + description
            task = self.queue.create_task(
                candidate.title, description=description,
                deadline_epoch=int(candidate.deadline_at.timestamp()) if candidate.deadline_at else None,
            )
            outcome_id = task.task_id
        elif candidate.kind is AgendaKind.APPOINTMENT:
            if candidate.start_at is None or candidate.end_at is None or candidate.uncertain:
                raise RuntimeError("unsafe_appointment_candidate")
            receipt = self.calendar.create_event(CalendarEventRequest(
                title=candidate.title, start_at=candidate.start_at, end_at=candidate.end_at,
                description=self._provenance_description(source),
            ))
            outcome_id = receipt.event_id
        else:
            self._remember_information(source, candidate, dedup_key)
            outcome_id = f"memory-{dedup_key[:24]}"
        payload = {"calendar_receipt": receipt.model_dump(mode="json") if receipt else None}
        self.store.put_item(dedup_key, candidate.kind, outcome_id, payload)
        self.store.link_source(dedup_key, source)
        self._remember_source(source, candidate, dedup_key, outcome_id)
        return AgendaResult(kind=candidate.kind, dedup_key=dedup_key, duplicate=False, outcome_id=outcome_id, calendar_receipt=receipt, candidate=candidate)

    def process_call_audio(self, content: bytes, *, mime_type: str, sender: str, call_id: str, timestamp: datetime, stt: LocalSTT) -> AgendaResult:
        transcript = _compact(stt.transcribe(content, mime_type))
        if not transcript:
            raise ValueError("empty_call_transcript")
        return self.process(AgendaSource(channel="phone_call", sender=sender, native_id=call_id, timestamp=timestamp, original_text=transcript))

    @staticmethod
    def _provenance_description(source: AgendaSource) -> str:
        return f"Bot-tazzi Agenda source={source.channel} sender={source.sender} native_id={source.native_id} timestamp={source.timestamp.isoformat()}\nOriginale: {source.original_text}"

    def _source_ref(self, source: AgendaSource) -> SourceRef:
        return SourceRef(
            system=source.channel, native_id=source.native_id,
            locator=f"{source.channel}:{source.native_id}", observed_at=source.timestamp.isoformat(),
            content_hash=hashlib.sha256(source.original_text.encode()).hexdigest(),
        )

    def _remember_source(self, source: AgendaSource, candidate: AgendaCandidate, dedup_key: str, outcome_id: str | None) -> None:
        ref = self._source_ref(source)
        event_id = "agenda-source-" + hashlib.sha256(f"{source.channel}\0{source.native_id}\0{dedup_key}".encode()).hexdigest()[:40]
        event = MemoryEvent.build(
            event_id=event_id, type="AGENDA_SOURCE", source=source.channel, source_id=source.native_id,
            occurred_at=source.timestamp, observed_at=source.timestamp,
            entity_refs=(f"agenda:{dedup_key}",),
            payload={"sender": source.sender, "original_text": source.original_text, "kind": candidate.kind.value, "dedup_key": dedup_key, "outcome_id": outcome_id},
            provenance=(ref,),
        )
        self.memory.append_event(event)

    def _remember_information(self, source: AgendaSource, candidate: AgendaCandidate, dedup_key: str) -> None:
        document = MemoryDocument.build(
            document_id=f"agenda-info-{dedup_key[:32]}", title=candidate.title[:500],
            body=source.original_text, source=self._source_ref(source),
        )
        self.memory.put_document(document)
