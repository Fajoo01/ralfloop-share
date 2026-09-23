from __future__ import annotations

from collections import Counter
from functools import lru_cache
import os
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ralfloop_agent.unified_assistant.accounting_external_evidence import load_evidence_snapshot
from ralfloop_agent.unified_assistant.accounting_review_store import (
    AccountingReviewDecisionStore,
    review_item_sha256,
)
from ralfloop_agent.unified_assistant.accounting_runts_live import (
    audit_runts_missing_documents,
    open_runts_readonly,
)


router = APIRouter(prefix="/accounting/review", tags=["assistant-v1-accounting-review"])

DecisionName = Literal[
    "approve_reconstruction",
    "approve_nonreportable",
    "confirm_internal_transfer",
    "reject",
]
ReviewView = Literal["pending", "unresolved", "decided", "all"]


class AccountingDecisionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    movement_id: str = Field(min_length=1, max_length=80)
    item_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: DecisionName


class AccountingDecisionBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    year: int = Field(ge=2000, le=2100)
    batch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    items: list[AccountingDecisionItem] = Field(min_length=1, max_length=50)


def _runts_db_path() -> Path:
    raw = os.getenv("BOTTAZZI_RUNTS_DB_PATH", "").strip()
    if not raw:
        raise HTTPException(status_code=503, detail="accounting_runts_source_not_configured")
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise HTTPException(status_code=503, detail="accounting_runts_source_unavailable")
    return path


def _evidence_snapshot() -> dict[str, Any] | None:
    raw = os.getenv("BOTTAZZI_ACCOUNTING_EVIDENCE_SNAPSHOT", "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise HTTPException(status_code=503, detail="accounting_evidence_snapshot_unavailable")
    try:
        return load_evidence_snapshot(path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="accounting_evidence_snapshot_invalid") from exc


@lru_cache(maxsize=8)
def _decision_store(path: str) -> AccountingReviewDecisionStore:
    return AccountingReviewDecisionStore(path)


def get_accounting_decision_store() -> AccountingReviewDecisionStore:
    configured = os.getenv("BOTTAZZI_ACCOUNTING_REVIEW_DB", "").strip()
    key = str(Path(configured).expanduser()) if configured else ""
    return _decision_store(key) if key else AccountingReviewDecisionStore()


def _base_audit(year: int) -> dict[str, Any]:
    try:
        return audit_runts_missing_documents(
            _runts_db_path(),
            year=year,
            evidence_snapshot=_evidence_snapshot(),
            human_decisions=None,
            limit=None,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail="accounting_runts_source_unavailable") from exc


def _available_years() -> list[int]:
    with open_runts_readonly(_runts_db_path()) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT CAST(substr(data_movimento,1,4) AS INTEGER) AS year
            FROM movements
            WHERE importo_signed < 0
              AND length(data_movimento) >= 4
            ORDER BY year DESC
            """
        ).fetchall()
    return [int(row[0]) for row in rows if row[0] is not None and 2000 <= int(row[0]) <= 2100]


def _year_catalog() -> dict[str, Any]:
    years = []
    for year in _available_years():
        audit = _base_audit(year)
        summary = dict(audit.get("summary") or {})
        backlog = int(summary.get("missing_original_count") or 0)
        years.append({
            "year": year,
            "review_case_count": int(summary.get("expense_movement_count") or 0),
            "ready_count": int(summary.get("ready_for_human_confirmation_count") or 0),
            "unresolved_count": int(summary.get("unresolved_evidence_count") or 0),
            "backlog_count": backlog,
        })
    default_year = None
    if years:
        default_year = max(years, key=lambda row: (row["backlog_count"], row["year"]))["year"]
    return {"years": years, "default_year": default_year}


def _resolve_year(year: int | None) -> int:
    if year is not None:
        return int(year)
    catalog = _year_catalog()
    if catalog["default_year"] is None:
        raise HTTPException(status_code=404, detail="accounting_review_year_missing")
    return int(catalog["default_year"])


def _decorate_rows(
    audit: dict[str, Any], *, year: int, store: AccountingReviewDecisionStore
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], int]:
    decisions = store.list_year(year)
    rows: list[dict[str, Any]] = []
    stale_count = 0
    for raw in audit.get("rows") or ():
        row = dict(raw)
        movement_id = str(row.get("movement_id") or "")
        digest = review_item_sha256(row)
        stored = decisions.get(movement_id)
        current = None
        stale = False
        if stored:
            if str(stored.get("item_sha256") or "") == digest:
                current = dict(stored)
            else:
                stale = True
                stale_count += 1
        row["item_sha256"] = digest
        row["current_decision"] = current
        row["stored_decision_stale"] = stale
        rows.append(row)
    return rows, decisions, stale_count


def _filter_rows(rows: list[dict[str, Any]], view: ReviewView) -> list[dict[str, Any]]:
    if view == "pending":
        return [
            row for row in rows
            if row.get("ready_for_human_confirmation")
            and row.get("original_document_status") == "MISSING"
            and not row.get("current_decision")
        ]
    if view == "unresolved":
        return [
            row for row in rows
            if row.get("original_document_status") == "MISSING"
            and not row.get("ready_for_human_confirmation")
        ]
    if view == "decided":
        return [row for row in rows if row.get("current_decision")]
    return rows


def _decision_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = Counter(
        str(row["current_decision"]["decision"])
        for row in rows if row.get("current_decision")
    )
    return dict(sorted(counts.items()))


@router.get("/years")
def accounting_review_years() -> dict[str, Any]:
    catalog = _year_catalog()
    return {"ok": True, **catalog, "runts_writes": 0}


@router.get("")
def accounting_review_list(
    year: int | None = Query(None, ge=2000, le=2100),
    view: ReviewView = Query("pending"),
    offset: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=100),
) -> dict[str, Any]:
    resolved_year = _resolve_year(year)
    audit = _base_audit(resolved_year)
    store = get_accounting_decision_store()
    rows, _, stale_count = _decorate_rows(audit, year=resolved_year, store=store)
    filtered = _filter_rows(rows, view)
    batch = dict(audit.get("human_confirmation_batch") or {})
    valid_decided = sum(1 for row in rows if row.get("current_decision"))
    pending_ready = sum(
        1 for row in rows
        if row.get("ready_for_human_confirmation")
        and row.get("original_document_status") == "MISSING"
        and not row.get("current_decision")
    )
    selected = filtered[offset : offset + limit]
    summary = dict(audit.get("summary") or {})
    summary.update({
        "decision_store_count": valid_decided,
        "stale_decision_count": stale_count,
        "pending_ready_count": pending_ready,
        "decision_counts": _decision_counts(rows),
    })
    return {
        "ok": True,
        "year": resolved_year,
        "view": view,
        "offset": offset,
        "limit": limit,
        "count": len(selected),
        "total": len(filtered),
        "has_more": offset + len(selected) < len(filtered),
        "batch_sha256": str(batch.get("batch_sha256") or ""),
        "batch_item_count": int(batch.get("item_count") or 0),
        "batch_total_eur": str(batch.get("total_eur") or "0"),
        "summary": summary,
        "rows": selected,
        "source_mode": "runts_read_only+local_human_decisions",
        "runts_writes": 0,
        "payments": 0,
        "filings": 0,
    }


@router.post("/decisions")
def accounting_review_decide(request: AccountingDecisionBatchRequest) -> dict[str, Any]:
    audit = _base_audit(request.year)
    batch = dict(audit.get("human_confirmation_batch") or {})
    current_batch_sha = str(batch.get("batch_sha256") or "")
    if current_batch_sha != request.batch_sha256:
        raise HTTPException(status_code=409, detail="accounting_review_batch_stale")

    by_id = {str(row.get("movement_id") or ""): row for row in audit.get("rows") or ()}
    prepared: list[dict[str, str]] = []
    for item in request.items:
        row = by_id.get(item.movement_id)
        if row is None:
            raise HTTPException(status_code=409, detail="accounting_review_item_missing")
        if not row.get("ready_for_human_confirmation") or row.get("original_document_status") != "MISSING":
            raise HTTPException(status_code=409, detail="accounting_review_item_not_ready")
        current_item_sha = review_item_sha256(row)
        if current_item_sha != item.item_sha256:
            raise HTTPException(status_code=409, detail="accounting_review_item_stale")
        if item.decision == "confirm_internal_transfer" and str(
            row.get("suggested_human_decision") or ""
        ) != "confirm_internal_transfer":
            raise HTTPException(status_code=400, detail="accounting_review_transfer_evidence_missing")
        prepared.append(item.model_dump())

    store = get_accounting_decision_store()
    try:
        applied = store.apply_many(
            year=request.year,
            batch_sha256=request.batch_sha256,
            items=prepared,
            actor_surface="bottazzi_app",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "year": request.year,
        "applied_count": len(applied),
        "decisions": applied,
        "batch_sha256": request.batch_sha256,
        "runts_writes": 0,
        "decision_store_writes": len(applied),
        "payments": 0,
        "filings": 0,
    }


__all__ = [
    "AccountingDecisionBatchRequest",
    "AccountingDecisionItem",
    "accounting_review_decide",
    "accounting_review_list",
    "get_accounting_decision_store",
    "router",
]
