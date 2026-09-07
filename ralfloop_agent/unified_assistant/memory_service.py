from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from .contracts import Identifier, StrictModel
from .platform import SourceRef
from .tiremm_admin import Practice, SourceRecord


class MemoryEvent(StrictModel):
    event_id: Identifier
    type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,95}$")
    source: Identifier
    source_id: str = Field(min_length=1, max_length=500)
    occurred_at: datetime
    observed_at: datetime
    entity_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provenance: tuple[SourceRef, ...] = Field(min_length=1, max_length=16)

    @classmethod
    def build(cls, *, event_id: str, type: str, source: str, source_id: str, occurred_at: datetime, observed_at: datetime, entity_refs: tuple[str, ...] = (), payload: dict[str, Any], provenance: tuple[SourceRef, ...]) -> "MemoryEvent":
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return cls(event_id=event_id, type=type, source=source, source_id=source_id, occurred_at=occurred_at, observed_at=observed_at, entity_refs=entity_refs, payload=payload, content_hash=digest, provenance=provenance)


class MemoryDocument(StrictModel):
    document_id: Identifier
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=200_000)
    source: SourceRef
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def build(cls, *, document_id: str, title: str, body: str, source: SourceRef) -> "MemoryDocument":
        return cls(document_id=document_id, title=title, body=body, source=source, content_hash=hashlib.sha256(body.encode()).hexdigest())


class MemoryEntity(StrictModel):
    entity_id: str = Field(min_length=1, max_length=240)
    domain: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    entity_type: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,95}$")
    status: str = Field(min_length=1, max_length=96)
    updated_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    data: dict[str, Any]
    provenance: tuple[SourceRef, ...] = Field(min_length=1, max_length=16)

    @classmethod
    def build(cls, *, entity_id: str, domain: str, entity_type: str, status: str, updated_at: datetime, data: dict[str, Any], provenance: tuple[SourceRef, ...]) -> "MemoryEntity":
        digest = hashlib.sha256(_json(data).encode()).hexdigest()
        return cls(entity_id=entity_id, domain=domain, entity_type=entity_type, status=status, updated_at=updated_at, content_hash=digest, data=data, provenance=provenance)


class MemoryService:
    """Local authoritative projection. Raw external systems remain source authority."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self._migrate()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MemoryService":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def append_event(self, event: MemoryEvent) -> bool:
        payload = event.model_dump(mode="json")
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO events(event_id,event_type,source,source_id,occurred_at,observed_at,content_hash,record_json) VALUES(?,?,?,?,?,?,?,?)",
                    (event.event_id, event.type, event.source, event.source_id, event.occurred_at.isoformat(), event.observed_at.isoformat(), event.content_hash, _json(payload)),
                )
                for ref in event.entity_refs:
                    self.connection.execute("INSERT INTO event_entities(event_id,entity_ref) VALUES(?,?)", (event.event_id, ref))
        except sqlite3.IntegrityError:
            existing = self.connection.execute("SELECT record_json FROM events WHERE event_id=? OR (source=? AND source_id=? AND content_hash=?)", (event.event_id, event.source, event.source_id, event.content_hash)).fetchone()
            if existing and json.loads(existing["record_json"]) == payload:
                return False
            raise ValueError("memory_event_conflict") from None
        return True

    def get_event(self, event_id: str) -> MemoryEvent | None:
        row = self.connection.execute(
            "SELECT record_json FROM events WHERE event_id=?", (event_id,),
        ).fetchone()
        return MemoryEvent.model_validate_json(row[0]) if row else None

    def timeline(self, entity_ref: str, *, limit: int = 100) -> tuple[MemoryEvent, ...]:
        _limit(limit)
        rows = self.connection.execute(
            "SELECT e.record_json FROM events e JOIN event_entities x ON x.event_id=e.event_id WHERE x.entity_ref=? ORDER BY e.occurred_at DESC,e.event_id DESC LIMIT ?",
            (entity_ref, limit),
        ).fetchall()
        return tuple(MemoryEvent.model_validate_json(row["record_json"]) for row in rows)

    def put_practice(self, practice: Practice) -> None:
        payload = practice.model_dump(mode="json")
        with self.connection:
            current = self.connection.execute("SELECT updated_at FROM practices WHERE practice_id=?", (practice.practice_id,)).fetchone()
            if current and datetime.fromisoformat(current["updated_at"]) > practice.updated_at:
                raise ValueError("memory_practice_stale")
            self.connection.execute(
                "INSERT INTO practices(practice_id,status,updated_at,record_json) VALUES(?,?,?,?) ON CONFLICT(practice_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at,record_json=excluded.record_json",
                (practice.practice_id, practice.status, practice.updated_at.isoformat(), _json(payload)),
            )

    def put_source_record(self, record: SourceRecord) -> bool:
        payload = record.model_dump(mode="json")
        existing = self.connection.execute("SELECT record_json FROM admin_sources WHERE evidence_id=?", (record.evidence_id,)).fetchone()
        if existing:
            if json.loads(existing["record_json"]) != payload:
                raise ValueError("memory_source_conflict")
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO admin_sources(evidence_id,source_kind,source_id,content_hash,record_json) VALUES(?,?,?,?,?)",
                (record.evidence_id, record.source_kind, record.source_id, record.content_sha256, _json(payload)),
            )
        return True

    def list_source_records(self) -> tuple[SourceRecord, ...]:
        rows = self.connection.execute("SELECT record_json FROM admin_sources ORDER BY evidence_id").fetchall()
        return tuple(SourceRecord.model_validate_json(row["record_json"]) for row in rows)

    def get_practice(self, practice_id: str) -> Practice | None:
        row = self.connection.execute("SELECT record_json FROM practices WHERE practice_id=?", (practice_id,)).fetchone()
        return Practice.model_validate_json(row["record_json"]) if row else None

    def list_practices(self, *, status: str | None = None, limit: int = 100) -> tuple[Practice, ...]:
        _limit(limit)
        sql, args = "SELECT record_json FROM practices", []
        if status:
            sql, args = sql + " WHERE status=?", [status]
        rows = self.connection.execute(sql + " ORDER BY updated_at DESC,practice_id LIMIT ?", (*args, limit)).fetchall()
        return tuple(Practice.model_validate_json(row["record_json"]) for row in rows)

    def put_document(self, document: MemoryDocument) -> bool:
        existing = self.connection.execute("SELECT content_hash FROM documents WHERE document_id=?", (document.document_id,)).fetchone()
        if existing and existing["content_hash"] == document.content_hash:
            return False
        with self.connection:
            self.connection.execute(
                "INSERT INTO documents(document_id,title,body,content_hash,source_json) VALUES(?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET title=excluded.title,body=excluded.body,content_hash=excluded.content_hash,source_json=excluded.source_json",
                (document.document_id, document.title, document.body, document.content_hash, document.source.model_dump_json()),
            )
        return True

    def search_documents(self, query: str, *, limit: int = 20) -> tuple[MemoryDocument, ...]:
        _limit(limit)
        match = _fts_query(query)
        if not match:
            return ()
        rows = self.connection.execute(
            "SELECT d.* FROM documents_fts f JOIN documents d ON d.rowid=f.rowid WHERE documents_fts MATCH ? ORDER BY bm25(documents_fts),d.document_id LIMIT ?",
            (match, limit),
        ).fetchall()
        return tuple(MemoryDocument(document_id=row["document_id"], title=row["title"], body=row["body"], content_hash=row["content_hash"], source=SourceRef.model_validate_json(row["source_json"])) for row in rows)

    def put_entity(self, entity: MemoryEntity) -> bool:
        existing = self.connection.execute(
            "SELECT content_hash,updated_at FROM memory_entities WHERE entity_id=?",
            (entity.entity_id,),
        ).fetchone()
        if existing and existing["content_hash"] == entity.content_hash:
            return False
        if existing and datetime.fromisoformat(existing["updated_at"]) > entity.updated_at:
            raise ValueError("memory_entity_stale")
        with self.connection:
            self.connection.execute(
                "INSERT INTO memory_entities(entity_id,domain,entity_type,status,updated_at,content_hash,record_json) VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_id) DO UPDATE SET domain=excluded.domain,entity_type=excluded.entity_type,status=excluded.status,updated_at=excluded.updated_at,content_hash=excluded.content_hash,record_json=excluded.record_json",
                (entity.entity_id, entity.domain, entity.entity_type, entity.status, entity.updated_at.isoformat(), entity.content_hash, entity.model_dump_json()),
            )
        return True

    def get_entity(self, entity_id: str) -> MemoryEntity | None:
        row = self.connection.execute(
            "SELECT record_json FROM memory_entities WHERE entity_id=?", (entity_id,),
        ).fetchone()
        return MemoryEntity.model_validate_json(row["record_json"]) if row else None

    def list_entities(self, *, domain: str, status: str | None = None, limit: int = 100) -> tuple[MemoryEntity, ...]:
        _limit(limit)
        sql = "SELECT record_json FROM memory_entities WHERE domain=?"
        args: list[Any] = [domain]
        if status is not None:
            sql += " AND status=?"
            args.append(status)
        rows = self.connection.execute(
            sql + " ORDER BY updated_at DESC,entity_id LIMIT ?", (*args, limit),
        ).fetchall()
        return tuple(MemoryEntity.model_validate_json(row["record_json"]) for row in rows)

    def _migrate(self) -> None:
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY,event_type TEXT NOT NULL,source TEXT NOT NULL,source_id TEXT NOT NULL,occurred_at TEXT NOT NULL,observed_at TEXT NOT NULL,content_hash TEXT NOT NULL,record_json TEXT NOT NULL,UNIQUE(source,source_id,content_hash));
        CREATE TABLE IF NOT EXISTS event_entities(event_id TEXT NOT NULL REFERENCES events(event_id),entity_ref TEXT NOT NULL,PRIMARY KEY(event_id,entity_ref));
        CREATE INDEX IF NOT EXISTS event_entities_ref ON event_entities(entity_ref);
        CREATE TABLE IF NOT EXISTS practices(practice_id TEXT PRIMARY KEY,status TEXT NOT NULL,updated_at TEXT NOT NULL,record_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS admin_sources(evidence_id TEXT PRIMARY KEY,source_kind TEXT NOT NULL,source_id TEXT NOT NULL,content_hash TEXT NOT NULL,record_json TEXT NOT NULL,UNIQUE(source_kind,source_id,content_hash));
        CREATE TABLE IF NOT EXISTS documents(document_id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,content_hash TEXT NOT NULL,source_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS memory_entities(entity_id TEXT PRIMARY KEY,domain TEXT NOT NULL,entity_type TEXT NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL,content_hash TEXT NOT NULL,record_json TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS memory_entities_domain_status ON memory_entities(domain,status,updated_at);
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(title,body,content=documents,content_rowid=rowid);
        CREATE TRIGGER IF NOT EXISTS documents_ai AFTER INSERT ON documents BEGIN INSERT INTO documents_fts(rowid,title,body) VALUES(new.rowid,new.title,new.body); END;
        CREATE TRIGGER IF NOT EXISTS documents_ad AFTER DELETE ON documents BEGIN INSERT INTO documents_fts(documents_fts,rowid,title,body) VALUES('delete',old.rowid,old.title,old.body); END;
        CREATE TRIGGER IF NOT EXISTS documents_au AFTER UPDATE ON documents BEGIN INSERT INTO documents_fts(documents_fts,rowid,title,body) VALUES('delete',old.rowid,old.title,old.body); INSERT INTO documents_fts(rowid,title,body) VALUES(new.rowid,new.title,new.body); END;
        """)


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _limit(value: int) -> None:
    if not 1 <= value <= 100:
        raise ValueError("memory_limit_invalid")


def _fts_query(value: str) -> str:
    terms = re.findall(r"[\wÀ-ÿ-]+", value, flags=re.UNICODE)[:32]
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)


__all__ = ["MemoryDocument", "MemoryEntity", "MemoryEvent", "MemoryService"]
