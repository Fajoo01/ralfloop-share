from __future__ import annotations

import json
from pathlib import Path

import pytest

from ralfloop_agent.unified_assistant.arci_datatables import (
    ArciCardListItem,
    ArciDataTablesService,
    ArciListQuery,
)
from ralfloop_agent.unified_assistant.arci_point_reads import ArciPointReadError
from ralfloop_agent.unified_assistant.platform import ErrorCode
from scripts.ralf_arci_mcp_server import ArciMCPServer
from tests.test_arci_mcp import FakeProvider


FIXTURES = Path(__file__).parent / "fixtures" / "arci"


def _member(native_id: str):
    return {"id": f"row-{native_id}", "hydra_data": {"user": {"id": native_id}}}


def _card(native_id: str, user_id: str, status: int = 20):
    return {
        "id": native_id, "user_id": user_id, "number": f"N-{native_id}",
        "validity": 2026, "status": status, "preregistration": status == 10,
    }


class SanitizedFixtureTransport:
    test_class = "SANITIZED_FIXTURE"

    def __init__(self):
        self.member_pages = [[_member("user-1"), _member("user-2")], [_member("user-3")]]
        self.card_pages = [[_card("card-1", "user-1", 10), _card("card-2", "user-2")], [_card("card-3", "user-3")]]

    @staticmethod
    def _page(rows, page):
        return {"data": rows[page - 1], "meta": {
            "current_page": page, "last_page": len(rows),
            "per_page": 2, "total": sum(map(len, rows)),
        }}

    def list_members_page(self, query, page):
        return self._page(self.member_pages, page)

    def list_cards_page(self, query, page):
        return self._page(self.card_pages, page)


def test_real_contract_fixtures_are_sanitized_and_preserve_pagination_schema():
    for name, path in (
        ("users", "/api/backoffice/list_users/datatables"),
        ("cards", "/api/backoffice/cards/datatables"),
    ):
        fixture = json.loads((FIXTURES / f"{name if name == 'cards' else 'list_users'}_datatables.real_contract.sanitized.json").read_text())
        capture = fixture["captures"][0]
        assert capture["test_class"] == "REAL_CONTRACT"
        assert capture["method"] == "POST"
        assert capture["path"] == path
        assert capture["headers"]["content_type"] == "application/json"
        assert capture["payload"]["page"] == 1
        assert capture["response"]["meta"]["total"] == capture["response"]["captured_row_count"]
        serialized = json.dumps(fixture)
        assert "Bearer " not in serialized
        assert "@gmail." not in serialized


def test_complete_enumeration_reconciles_unique_ids_and_pending_cards():
    service = ArciDataTablesService(SanitizedFixtureTransport())
    query = ArciListQuery(club_id="club-1", validity=2026)

    members = service.list_members(query)
    cards = service.list_cards(query)
    pending = service.list_pending_card_requests(query)

    assert members.complete and members.expected_total == members.received_unique_ids == 3
    assert cards.complete and cards.pages == 2
    assert [row.id for row in pending.items] == ["card-1"]
    assert isinstance(cards.items[0], ArciCardListItem)
    assert cards.items[0].source.locator == "/cards/datatables?page=1"


@pytest.mark.parametrize("failure", ["duplicate", "repeated", "empty", "changed_total"])
def test_pagination_fails_closed(failure):
    transport = SanitizedFixtureTransport()
    if failure == "duplicate":
        transport.member_pages[1][0] = _member("user-1")
    elif failure == "repeated":
        transport.member_pages[1] = list(transport.member_pages[0])
    elif failure == "empty":
        transport.member_pages[0] = []
    else:
        original = transport.list_members_page
        transport.list_members_page = lambda query, page: {
            **original(query, page),
            "meta": {**original(query, page)["meta"], "total": 3 if page == 1 else 4},
        }

    with pytest.raises(ArciPointReadError) as exc:
        ArciDataTablesService(transport).list_members(ArciListQuery(club_id="club-1"))
    assert exc.value.code in {ErrorCode.CONFLICT, ErrorCode.INCOMPLETE_SOURCE, ErrorCode.STALE_DATA}


def test_mcp_complete_tools_require_paginator_injection():
    query = {"club_id": "club-1", "validity": 2026}
    disabled = ArciMCPServer(FakeProvider())
    enabled = ArciMCPServer(FakeProvider(), datatables=ArciDataTablesService(SanitizedFixtureTransport()))

    assert "arci_list_members" not in {row["name"] for row in disabled.list_tools()}
    assert "arci_list_members" in {row["name"] for row in enabled.list_tools()}
    result = enabled.call("arci_list_members", query)
    assert result["structuredContent"]["data"]["complete"] is True
    assert result["structuredContent"]["writes"] == 0
    assert enabled.call("arci_list_members", {**query, "url": "https://example.invalid"})["isError"] is True
