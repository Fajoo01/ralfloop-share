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
    idempotency_key: str = Field(default="", max_length=1000)


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
_MONTHS = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}
_HOUR_WORDS = {
    "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7, "otto": 8,
    "nove": 9, "dieci": 10, "undici": 11, "dodici": 12, "tredici": 13, "quattordici": 14,
    "quindici": 15, "sedici": 16, "diciassette": 17, "diciotto": 18, "diciannove": 19, "venti": 20,
    "ventuno": 21, "ventidue": 22, "ventitre": 23, "ventitré": 23,
}
_UNKNOWN_SENDERS = {"unknown", "sconosciuto", "anonimo", "numero privato", "private", "n/d"}
_UNCERTAIN_RE = re.compile(r"\b(forse|magari|probabilmente|eventualmente|potremmo|potrei|potremmo sentirci|se riesco|da confermare|dovremmo|vediamo se)\b", re.I)
_TIME_RE = re.compile(r"\b(?:alle|ore|verso\s+le|per\s+le|dalle)\s*(\d{1,2})(?:[:.]([0-5]\d))?\b", re.I)
_BARE_TIME_RE = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)")
_WORD_TIME_RE = re.compile(r"\b(?:alle|ore|verso\s+le|per\s+le)\s+(" + "|".join(map(re.escape, _HOUR_WORDS)) + r")(?:\s+e\s+(mezza|un\s+quarto|quarto))?(?:\s+di\s+(mattina|pomeriggio|sera))?\b", re.I)
_MEETING_RE = re.compile(r"\b(ci\s+vediamo|vediamoci|appuntamento|incontro|riunione|call|videochiamata|telefonata|ci\s+sentiamo|colloquio|prenotazione)\b", re.I)
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
    target = None
    if re.search(r"\b(oggi|stasera|questa sera)\b", low):
        target = ref.date()
    elif re.search(r"\bdopodomani\b", low):
        target = (ref + timedelta(days=2)).date()
    elif re.search(r"\bdomani\b", low):
        target = (ref + timedelta(days=1)).date()
    else:
        after = re.search(r"\btra\s+(\d{1,3})\s+giorni?\b", low)
        if after:
            target = (ref + timedelta(days=int(after.group(1)))).date()
    if target is None:
        numeric = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", low)
        named = re.search(r"\b(?:il\s+)?(\d{1,2})\s+(" + "|".join(_MONTHS) + r")(?:\s+(\d{4}))?\b", low)
        try:
            if numeric:
                day, month = int(numeric.group(1)), int(numeric.group(2))
                raw_year = numeric.group(3)
                year = int(raw_year) if raw_year else ref.year
                if raw_year and year < 100:
                    year += 2000
                target = datetime(year, month, day, tzinfo=tz).date()
                if not raw_year and target < ref.date():
                    target = datetime(year + 1, month, day, tzinfo=tz).date()
            elif named:
                day, month = int(named.group(1)), _MONTHS[named.group(2)]
                raw_year = named.group(3)
                year = int(raw_year) if raw_year else ref.year
                target = datetime(year, month, day, tzinfo=tz).date()
                if not raw_year and target < ref.date():
                    target = datetime(year + 1, month, day, tzinfo=tz).date()
        except ValueError:
            target = None
    if target is None:
        for name, weekday in _WEEKDAYS.items():
            if re.search(rf"\b{re.escape(name)}\b", low):
                delta = (weekday - ref.weekday()) % 7
                target = (ref + timedelta(days=delta)).date()
                break
    return datetime.combine(target, time(0, 0), tzinfo=tz) if target is not None else None


def _clock(text: str) -> tuple[int, int] | None:
    match = _TIME_RE.search(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2) or 0)
        if hour > 23:
            return None
        tail = text[match.end(): match.end() + 24].casefold()
        if hour < 12 and re.search(r"\b(?:di\s+)?(?:pomeriggio|sera)\b", tail):
            hour += 12
        return hour, minute
    words = _WORD_TIME_RE.search(text)
    if words:
        hour = _HOUR_WORDS[words.group(1).casefold()]
        minute = 30 if words.group(2) and "mezza" in words.group(2).casefold() else 15 if words.group(2) else 0
        period = (words.group(3) or "").casefold()
        if hour < 12 and period in {"pomeriggio", "sera"}:
            hour += 12
        return hour, minute
    bare = _BARE_TIME_RE.search(text)
    return (int(bare.group(1)), int(bare.group(2))) if bare else None


def _when(text: str, reference: datetime, *, end_of_day: bool = False) -> datetime | None:
    day = _day(text, reference)
    if day is None:
        return None
    clock = _clock(text)
    if clock:
        return day.replace(hour=clock[0], minute=clock[1])
    return day.replace(hour=23, minute=59) if end_of_day else None


def _meeting_end(text: str, start: datetime) -> datetime:
    range_match = re.search(r"\bdalle\s+\d{1,2}(?:[:.]\d{2})?\s+(?:fino\s+)?alle\s+(\d{1,2})(?:[:.]([0-5]\d))?\b", text, re.I)
    until_match = re.search(r"\bfino\s+alle\s+(\d{1,2})(?:[:.]([0-5]\d))?\b", text, re.I)
    match = range_match or until_match
    if match:
        end = start.replace(hour=int(match.group(1)), minute=int(match.group(2) or 0))
        return end if end > start else end + timedelta(days=1)
    duration = re.search(r"\bper\s+(\d+(?:[.,]\d+)?)\s*(ore?|minuti?)\b", text, re.I)
    if duration:
        amount = float(duration.group(1).replace(",", "."))
        return start + (timedelta(hours=amount) if duration.group(2).casefold().startswith("or") else timedelta(minutes=amount))
    return start + timedelta(hours=1)


def _party(source: AgendaSource, text: str) -> str | None:
    match = re.search(r"\bcon\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’-]{1,40}(?:\s+[A-ZÀ-Ý][A-Za-zÀ-ÿ'’-]{1,40})?)", text)
    if match:
        explicit = match.group(1).strip()
        if _normal(explicit) not in {"te", "voi", "lui", "lei", "noi", "un", "una"}:
            return explicit
    sender = _compact(source.sender).strip(" <>,-")
    return sender if _normal(sender) not in _UNKNOWN_SENDERS else None


def _semantic_core(text: str) -> str:
    value = _normal(text)
    value = re.sub(r"\b(oggi|domani|dopodomani|stasera|questa sera|lunedi|lunedì|martedi|martedì|mercoledi|mercoledì|giovedi|giovedì|venerdi|venerdì|sabato|domenica)\b", " ", value)
    value = re.sub(r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b", " ", value)
    value = re.sub(r"\b(?:il\s+)?\d{1,2}\s+(?:" + "|".join(_MONTHS) + r")(?:\s+\d{4})?\b", " ", value)
    value = re.sub(r"\b(?:alle|ore|verso le|per le|dalle)\s*\d{1,2}(?:[:.]\d{2})?\b", " ", value)
    return " ".join(value.split())


def _semantic_key(kind: str, *parts: str) -> str:
    raw = "|".join((kind, *parts))
    if len(raw) <= 900:
        return raw
    return f"{kind}|sha256:{hashlib.sha256(raw.encode()).hexdigest()}"


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
                semantic_key=_semantic_key("task", _normal(action), deadline.isoformat() if deadline else ""),
            )
        if re.search(r"\b(avvisami|notificami)\b", text, re.I):
            when = _when(text, source.timestamp, end_of_day=True)
            return AgendaCandidate(
                kind=AgendaKind.NOTIFICATION, title=text, deadline_at=when,
                confidence=0.95 if not uncertain else 0.6, uncertain=uncertain,
                needs_motor=uncertain,
                semantic_key=_semantic_key("notification", _semantic_core(text), when.isoformat() if when else ""),
            )
        meeting_signal = bool(_MEETING_RE.search(text))
        start = _when(text, source.timestamp)
        if meeting_signal and start is not None:
            core = _semantic_core(text)
            party = _party(source, text)
            identity = _normal(party) if party else (core or "unknown")
            if uncertain:
                return AgendaCandidate(
                    kind=AgendaKind.INFORMATION, title=text, confidence=0.55,
                    uncertain=True, needs_motor=True,
                    semantic_key=_semantic_key("information", identity, start.isoformat()),
                )
            return AgendaCandidate(
                kind=AgendaKind.APPOINTMENT,
                title=f"Incontro con {party}" if party else "Appuntamento",
                start_at=start, end_at=_meeting_end(text, start), confidence=0.99,
                semantic_key=_semantic_key("appointment", identity, start.isoformat()),
            )
        return AgendaCandidate(
            kind=AgendaKind.INFORMATION, title=text[:500], confidence=0.98,
            uncertain=uncertain, needs_motor=uncertain,
            semantic_key=_semantic_key("information", _normal(text)),
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


def _calendar_event_id(event: CalendarEventRequest) -> str:
    material = event.idempotency_key.strip() or "\0".join((
        _normal(event.title), event.start_at.astimezone(timezone.utc).isoformat(),
        event.end_at.astimezone(timezone.utc).isoformat(),
    ))
    return "agenda-" + hashlib.sha256(material.encode()).hexdigest()[:32]


class LocalIcsCalendarProvider:
    """Persistent self-hosted fallback and deterministic test provider."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def create_event(self, event: CalendarEventRequest) -> CalendarReceipt:
        event_id = _calendar_event_id(event)
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
        event_id = _calendar_event_id(event)
        locator = f"{self.collection_url}{event_id}.ics"
        response = self.session.put(
            locator, data=_ics(event_id, event).encode("utf-8"),
            headers={"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
            auth=(self.username, self.password), timeout=self.timeout, verify=self.verify_tls,
        )
        try:
            if response.status_code not in {201, 204, 412}:
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
            calendar_key = dedup_key
            if source.channel == "phone_call":
                calendar_key = hashlib.sha256(f"phone_call\0{source.native_id}\0{candidate.start_at.isoformat()}".encode()).hexdigest()
            receipt = self.calendar.create_event(CalendarEventRequest(
                title=candidate.title, start_at=candidate.start_at, end_at=candidate.end_at,
                description=self._provenance_description(source), idempotency_key=calendar_key,
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
