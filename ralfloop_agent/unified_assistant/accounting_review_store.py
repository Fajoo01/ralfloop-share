from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Mapping


ALLOWED_ACCOUNTING_DECISIONS = frozenset({
    "approve_reconstruction",
    "approve_nonreportable",
    "confirm_internal_transfer",
    "reject",
})


def default_accounting_review_db_path() -> Path:
    configured = os.getenv("BOTTAZZI_ACCOUNTING_REVIEW_DB", "").strip()
    if configured:
        return Path(configured).expanduser()
    data_home = os.getenv("XDG_DATA_HOME", "").strip()
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return root / "bottazzi" / "accounting" / "review-decisions.sqlite"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def review_item_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    external = []
    for evidence in row.get("external_evidence") or ():
        if not isinstance(evidence, Mapping):
            continue
        external.append({
            "kind": str(evidence.get("kind") or ""),
            "provenance_ref": str(evidence.get("provenance_ref") or ""),
            "confidence": int(evidence.get("confidence") or 0),
            "fiscal_document": bool(evidence.get("fiscal_document")),
        })
    external.sort(key=lambda item: (item["kind"], item["provenance_ref"]))
    return {
        "movement_id": str(row.get("movement_id") or ""),
        "movement_date": str(row.get("movement_date") or ""),
        "amount_eur": str(row.get("amount_eur") or ""),
        "counterparty": str(row.get("counterparty") or ""),
        "description": str(row.get("description") or ""),
        "original_document_status": str(row.get("original_document_status") or ""),
        "reconstruction_evidence_score": int(row.get("reconstruction_evidence_score") or 0),
        "suggested_human_decision": str(row.get("suggested_human_decision") or "approve_reconstruction"),
        "source_duplicate_import_ids": sorted(int(value) for value in row.get("source_duplicate_import_ids") or ()),
        "source_duplicate_owner_unverified": bool(row.get("source_duplicate_owner_unverified")),
        "external_evidence": external,
    }


def review_item_sha256(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(review_item_payload(row)).encode("utf-8")).hexdigest()


class AccountingReviewDecisionStore:
    """Human decisions for Commercialista review; never mutates RUNTS Suite.

    The latest decision is projected in ``decisions`` while every change is retained
    in ``decision_history``. Evidence and batch hashes are stored with each decision.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or default_accounting_review_db_path()).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    year INTEGER NOT NULL,
                    movement_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    item_sha256 TEXT NOT NULL,
                    batch_sha256 TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    actor_surface TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    PRIMARY KEY(year, movement_id)
                );
                CREATE TABLE IF NOT EXISTS decision_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    year INTEGER NOT NULL,
                    movement_id TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    item_sha256 TEXT NOT NULL,
                    batch_sha256 TEXT NOT NULL,
                    decided_at TEXT NOT NULL,
                    actor_surface TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
                """
            )
            current = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if current and int(current[0]) != self.SCHEMA_VERSION:
                raise ValueError("accounting_review_schema_migration_required")
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key,value) VALUES('schema_version',?)",
                (str(self.SCHEMA_VERSION),),
            )
        os.chmod(self.path, 0o600)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def list_year(self, year: int) -> dict[str, dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decisions WHERE year=? ORDER BY movement_id", (int(year),)
            ).fetchall()
        return {str(row["movement_id"]): dict(row) for row in rows}

    def history(self, year: int, movement_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM decision_history WHERE year=? AND movement_id=? ORDER BY history_id",
                (int(year), str(movement_id)),
            ).fetchall()
        return [dict(row) for row in rows]

    def apply_many(
        self,
        *,
        year: int,
        batch_sha256: str,
        items: Iterable[Mapping[str, Any]],
        actor_surface: str = "bottazzi_app",
    ) -> list[dict[str, Any]]:
        batch_sha256 = str(batch_sha256).strip().lower()
        if len(batch_sha256) != 64:
            raise ValueError("accounting_review_batch_hash_invalid")
        prepared: list[dict[str, str]] = []
        for item in items:
            movement_id = str(item.get("movement_id") or "").strip()
            decision = str(item.get("decision") or "").strip()
            item_sha256 = str(item.get("item_sha256") or "").strip().lower()
            if not movement_id:
                raise ValueError("accounting_review_movement_id_required")
            if decision not in ALLOWED_ACCOUNTING_DECISIONS:
                raise ValueError("accounting_review_decision_invalid")
            if len(item_sha256) != 64:
                raise ValueError("accounting_review_item_hash_invalid")
            prepared.append({
                "movement_id": movement_id,
                "decision": decision,
                "item_sha256": item_sha256,
            })
        if not prepared:
            raise ValueError("accounting_review_items_required")
        if len({item["movement_id"] for item in prepared}) != len(prepared):
            raise ValueError("accounting_review_duplicate_movement")

        timestamp = datetime.now(UTC).isoformat()
        output: list[dict[str, Any]] = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item in prepared:
                    current = conn.execute(
                        "SELECT revision FROM decisions WHERE year=? AND movement_id=?",
                        (int(year), item["movement_id"]),
                    ).fetchone()
                    revision = int(current["revision"]) + 1 if current else 1
                    values = (
                        int(year), item["movement_id"], item["decision"], item["item_sha256"],
                        batch_sha256, timestamp, actor_surface, revision,
                    )
                    conn.execute(
                        """
                        INSERT INTO decisions(year,movement_id,decision,item_sha256,batch_sha256,decided_at,actor_surface,revision)
                        VALUES(?,?,?,?,?,?,?,?)
                        ON CONFLICT(year,movement_id) DO UPDATE SET
                            decision=excluded.decision,
                            item_sha256=excluded.item_sha256,
                            batch_sha256=excluded.batch_sha256,
                            decided_at=excluded.decided_at,
                            actor_surface=excluded.actor_surface,
                            revision=excluded.revision
                        """,
                        values,
                    )
                    conn.execute(
                        """
                        INSERT INTO decision_history(year,movement_id,decision,item_sha256,batch_sha256,decided_at,actor_surface,revision)
                        VALUES(?,?,?,?,?,?,?,?)
                        """,
                        values,
                    )
                    output.append({
                        "year": int(year), "movement_id": item["movement_id"],
                        "decision": item["decision"], "item_sha256": item["item_sha256"],
                        "batch_sha256": batch_sha256, "decided_at": timestamp,
                        "actor_surface": actor_surface, "revision": revision,
                    })
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return output


__all__ = [
    "ALLOWED_ACCOUNTING_DECISIONS",
    "AccountingReviewDecisionStore",
    "default_accounting_review_db_path",
    "review_item_payload",
    "review_item_sha256",
]
