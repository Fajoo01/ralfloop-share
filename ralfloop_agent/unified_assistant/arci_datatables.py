from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol

from pydantic import Field

from .arci_point_reads import ArciCardStatus, ArciPointReadError
from .contracts import StrictModel
from .platform import ErrorCode, SourceRef


class ArciListQuery(StrictModel):
    club_id: str = Field(min_length=1, max_length=240)
    committee_id: str | None = Field(default=None, max_length=240)
    regional_id: str | None = Field(default=None, max_length=240)
    validity: int | None = Field(default=None, ge=2000, le=2200)
    search: str | None = Field(default=None, max_length=200)
    page_size: int = Field(default=100, ge=1, le=100)


class ArciMemberListItem(StrictModel):
    id: str = Field(min_length=1, max_length=240)
    source: SourceRef


class ArciCardListItem(StrictModel):
    id: str = Field(min_length=1, max_length=240)
    user_id: str = Field(min_length=1, max_length=240)
    club_id: str = Field(min_length=1, max_length=240)
    number: str | None = Field(default=None, max_length=240)
    validity: int | None = Field(default=None, ge=2000, le=2200)
    status_raw: int
    status_normalized: ArciCardStatus
    preregistration: bool | None = None
    source: SourceRef


class ArciCompleteList(StrictModel):
    items: tuple[ArciMemberListItem | ArciCardListItem, ...]
    expected_total: int = Field(ge=0)
    received_unique_ids: int = Field(ge=0)
    pages: int = Field(ge=1)
    complete: bool


class ArciDataTablesTransport(Protocol):
    """Two fixed allowlisted reads; no generic HTTP method."""

    def list_members_page(self, query: ArciListQuery, page: int) -> Mapping[str, Any]: ...
    def list_cards_page(self, query: ArciListQuery, page: int) -> Mapping[str, Any]: ...


class ArciDataTablesService:
    def __init__(self, transport: ArciDataTablesTransport, *, max_pages: int = 1000) -> None:
        if not 1 <= max_pages <= 1000:
            raise ValueError("arci_max_pages_invalid")
        self.transport = transport
        self.max_pages = max_pages

    def list_members(self, query: ArciListQuery) -> ArciCompleteList:
        rows, total, pages = self._enumerate(query, self.transport.list_members_page, _member_id)
        items = tuple(ArciMemberListItem(
            id=_member_id(row), source=_source("list_users/datatables", _member_id(row), row, page),
        ) for page, row in rows)
        return ArciCompleteList(
            items=items, expected_total=total, received_unique_ids=len(items),
            pages=pages, complete=True,
        )

    def list_cards(self, query: ArciListQuery) -> ArciCompleteList:
        rows, total, pages = self._enumerate(query, self.transport.list_cards_page, _row_id)
        items = tuple(_card_item(row, query.club_id, page) for page, row in rows)
        return ArciCompleteList(
            items=items, expected_total=total, received_unique_ids=len(items),
            pages=pages, complete=True,
        )

    def list_pending_card_requests(self, query: ArciListQuery) -> ArciCompleteList:
        complete = self.list_cards(query)
        pending = tuple(
            row for row in complete.items
            if isinstance(row, ArciCardListItem) and row.status_raw == 10
        )
        return ArciCompleteList(
            items=pending,
            expected_total=len(pending), received_unique_ids=len(pending),
            pages=complete.pages, complete=True,
        )

    def _enumerate(
        self,
        query: ArciListQuery,
        read_page: Callable[[ArciListQuery, int], Mapping[str, Any]],
        identity: Callable[[Mapping[str, Any]], str],
    ) -> tuple[list[tuple[int, Mapping[str, Any]]], int, int]:
        collected: list[tuple[int, Mapping[str, Any]]] = []
        seen_ids: set[str] = set()
        page_fingerprints: set[str] = set()
        expected_total: int | None = None
        expected_last: int | None = None
        page = 1
        while page <= self.max_pages:
            raw = read_page(query, page)
            data, meta = _envelope(raw)
            current = _integer(meta, "current_page", minimum=1)
            last = _integer(meta, "last_page", minimum=1)
            total = _integer(meta, "total", minimum=0)
            if current != page or last < current:
                raise ArciPointReadError(ErrorCode.INCOMPLETE_SOURCE, "pagination_position_invalid")
            if expected_total is None:
                expected_total, expected_last = total, last
            elif total != expected_total or last != expected_last:
                raise ArciPointReadError(ErrorCode.STALE_DATA, "pagination_totals_changed")
            ids = tuple(identity(row) for row in data)
            fingerprint = hashlib.sha256(json.dumps(ids).encode()).hexdigest()
            if fingerprint in page_fingerprints and data:
                raise ArciPointReadError(ErrorCode.INCOMPLETE_SOURCE, "pagination_page_repeated")
            page_fingerprints.add(fingerprint)
            for row, native_id in zip(data, ids, strict=True):
                if native_id in seen_ids:
                    raise ArciPointReadError(ErrorCode.CONFLICT, "pagination_duplicate_id")
                seen_ids.add(native_id)
                collected.append((page, row))
            if current == last:
                if len(seen_ids) != total:
                    raise ArciPointReadError(ErrorCode.INCOMPLETE_SOURCE, "pagination_total_mismatch")
                return collected, total, page
            if not data:
                raise ArciPointReadError(ErrorCode.INCOMPLETE_SOURCE, "pagination_empty_early_page")
            page += 1
        raise ArciPointReadError(ErrorCode.INCOMPLETE_SOURCE, "pagination_max_pages_exceeded")


def _envelope(raw: Any) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("data"), list) or not isinstance(raw.get("meta"), Mapping):
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "datatable_envelope_invalid")
    data = raw["data"]
    if any(not isinstance(row, Mapping) for row in data):
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "datatable_row_invalid")
    return data, raw["meta"]


def _integer(meta: Mapping[str, Any], key: str, *, minimum: int) -> int:
    value = meta.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, f"datatable_{key}_invalid")
    return value


def _row_id(row: Mapping[str, Any]) -> str:
    value = row.get("id")
    if not isinstance(value, str) or not value:
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "datatable_native_id_missing")
    return value


def _member_id(row: Mapping[str, Any]) -> str:
    hydra = row.get("hydra_data")
    user = hydra.get("user") if isinstance(hydra, Mapping) else None
    if not isinstance(user, Mapping):
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "datatable_user_relation_missing")
    return _row_id(user)


def _source(endpoint: str, native_id: str, row: Mapping[str, Any], page: int) -> SourceRef:
    canonical = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
    return SourceRef(
        system="arci", native_id=native_id, locator=f"/{endpoint}?page={page}",
        observed_at=datetime.now(timezone.utc).isoformat(),
        content_hash=hashlib.sha256(canonical.encode()).hexdigest(),
    )


def _card_item(row: Mapping[str, Any], club_id: str, page: int) -> ArciCardListItem:
    try:
        status = int(row["status"])
        validity = int(row["validity"]) if row.get("validity") is not None else None
        user_id = str(row["user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ArciPointReadError(ErrorCode.MALFORMED_RESPONSE, "datatable_card_contract_invalid") from exc
    normalized = {10: ArciCardStatus.PENDING, 20: ArciCardStatus.APPROVED, 25: ArciCardStatus.EXPELLED, 30: ArciCardStatus.REJECTED}.get(status, ArciCardStatus.UNKNOWN)
    return ArciCardListItem(
        id=_row_id(row), user_id=user_id, club_id=club_id,
        number=str(row["number"]) if row.get("number") is not None else None,
        validity=validity, status_raw=status, status_normalized=normalized,
        preregistration=row.get("preregistration"),
        source=_source("cards/datatables", _row_id(row), row, page),
    )


__all__ = ["ArciCardListItem", "ArciCompleteList", "ArciDataTablesService", "ArciDataTablesTransport", "ArciListQuery", "ArciMemberListItem"]
