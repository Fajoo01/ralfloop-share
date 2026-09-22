from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from .models import EvidenceKind, RelationEvent, RelationSnapshot


SCHEMA_VERSION = 1


class RelationStore:
    """Small SQLite event store for the ABC relation domain.

    The DB stores normalized evidence and bounded excerpts, not full chat exports.
    Raw source files remain outside the database and are referenced by provenance.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS abc_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS abc_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    actor TEXT,
                    kind TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    raw_excerpt TEXT,
                    confidence REAL NOT NULL,
                    weight REAL NOT NULL,
                    tags_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    supersedes_event_id TEXT,
                    active INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (supersedes_event_id) REFERENCES abc_events(event_id)
                );
                CREATE INDEX IF NOT EXISTS idx_abc_events_time
                    ON abc_events(occurred_at DESC);
                CREATE INDEX IF NOT EXISTS idx_abc_events_kind
                    ON abc_events(kind, active, occurred_at DESC);
                CREATE TABLE IF NOT EXISTS abc_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    label TEXT NOT NULL,
                    state_summary TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_abc_snapshots_created
                    ON abc_snapshots(created_at DESC);
                """
            )
            conn.execute(
                "INSERT OR REPLACE INTO abc_meta(key,value) VALUES('schema_version',?)",
                (str(SCHEMA_VERSION),),
            )

    def put_event(self, event: RelationEvent) -> None:
        with self._connect() as conn:
            if event.supersedes_event_id:
                conn.execute(
                    "UPDATE abc_events SET active=0 WHERE event_id=?",
                    (event.supersedes_event_id,),
                )
            conn.execute(
                """
                INSERT INTO abc_events(
                    event_id, occurred_at, actor, kind, summary, raw_excerpt,
                    confidence, weight, tags_json, provenance_json,
                    supersedes_event_id, active
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(event_id) DO UPDATE SET
                    occurred_at=excluded.occurred_at,
                    actor=excluded.actor,
                    kind=excluded.kind,
                    summary=excluded.summary,
                    raw_excerpt=excluded.raw_excerpt,
                    confidence=excluded.confidence,
                    weight=excluded.weight,
                    tags_json=excluded.tags_json,
                    provenance_json=excluded.provenance_json,
                    supersedes_event_id=excluded.supersedes_event_id,
                    active=excluded.active
                """,
                (
                    event.event_id,
                    event.occurred_at.isoformat(),
                    event.actor,
                    event.kind.value,
                    event.summary,
                    event.raw_excerpt,
                    event.confidence,
                    event.weight,
                    json.dumps(event.tags, ensure_ascii=False),
                    event.provenance.model_dump_json(),
                    event.supersedes_event_id,
                    int(event.active),
                ),
            )

    def get_event(self, event_id: str) -> RelationEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM abc_events WHERE event_id=?", (event_id,)
            ).fetchone()
        return self._event_from_row(row) if row else None

    def list_events(
        self,
        *,
        limit: int = 100,
        kind: EvidenceKind | None = None,
        active_only: bool = True,
    ) -> tuple[RelationEvent, ...]:
        where: list[str] = []
        params: list[object] = []
        if active_only:
            where.append("active=1")
        if kind is not None:
            where.append("kind=?")
            params.append(kind.value)
        sql = "SELECT * FROM abc_events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY occurred_at DESC, event_id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def search_events(self, query: str, *, limit: int = 50) -> tuple[RelationEvent, ...]:
        needle = f"%{query.casefold()}%"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM abc_events
                WHERE active=1 AND (
                    lower(summary) LIKE ? OR lower(COALESCE(raw_excerpt,'')) LIKE ?
                    OR lower(tags_json) LIKE ?
                )
                ORDER BY occurred_at DESC, event_id DESC
                LIMIT ?
                """,
                (needle, needle, needle, limit),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def put_snapshot(self, snapshot: RelationSnapshot) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO abc_snapshots(snapshot_id,created_at,label,state_summary,payload_json)
                VALUES(?,?,?,?,?)
                ON CONFLICT(snapshot_id) DO UPDATE SET
                    created_at=excluded.created_at,
                    label=excluded.label,
                    state_summary=excluded.state_summary,
                    payload_json=excluded.payload_json
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.created_at.isoformat(),
                    snapshot.label,
                    snapshot.state_summary,
                    snapshot.model_dump_json(),
                ),
            )

    def latest_snapshot(self) -> RelationSnapshot | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM abc_snapshots ORDER BY created_at DESC, snapshot_id DESC LIMIT 1"
            ).fetchone()
        if not row:
            return None
        return RelationSnapshot.model_validate_json(row["payload_json"])

    def list_snapshots(self, *, limit: int = 20) -> tuple[RelationSnapshot, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM abc_snapshots ORDER BY created_at DESC, snapshot_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(RelationSnapshot.model_validate_json(row["payload_json"]) for row in rows)

    def counts(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) AS n FROM abc_events WHERE active=1 GROUP BY kind"
            ).fetchall()
        output = {kind.value: 0 for kind in EvidenceKind}
        output.update({str(row["kind"]): int(row["n"]) for row in rows})
        return output

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> RelationEvent:
        payload = {
            "event_id": row["event_id"],
            "occurred_at": row["occurred_at"],
            "actor": row["actor"],
            "kind": row["kind"],
            "summary": row["summary"],
            "raw_excerpt": row["raw_excerpt"],
            "confidence": row["confidence"],
            "weight": row["weight"],
            "tags": json.loads(row["tags_json"]),
            "provenance": json.loads(row["provenance_json"]),
            "supersedes_event_id": row["supersedes_event_id"],
            "active": bool(row["active"]),
        }
        return RelationEvent.model_validate(payload)

    def close(self) -> None:
        """Compatibility no-op: connections are short-lived and context managed."""

    def __enter__(self) -> "RelationStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
