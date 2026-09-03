from __future__ import annotations

import pytest

from ralfloop_agent.unified_assistant.arci_point_reads import (
    ArciCardStatus,
    ArciPointReadError,
    ArciPointReadService,
)
from ralfloop_agent.unified_assistant.platform import ErrorCode
from ralfloop_agent.unified_assistant.service_identity import VerificationOutcome
from scripts.ralf_arci_mcp_server import ArciMCPServer

from tests.test_arci_mcp import FakeProvider


class SanitizedFixtureTransport:
    test_class = "SANITIZED_FIXTURE"

    def __init__(self) -> None:
        self.member = {"id": "user-synthetic-001", "name": "Synthetic Person"}
        self.club = {"id": "club-synthetic-001", "validity_year": 2026}
        self.cards = [{
            "id": "card-synthetic-001",
            "user_id": self.member["id"],
            "club_id": self.club["id"],
            "number": "SYN-0001",
            "validity": 2026,
            "status": 20,
            "expired": False,
            "enabled_at": "2026-01-02T00:00:00Z",
            "disabled_at": None,
            "preregistration": False,
            "consumer_movement_status": None,
        }]

    def get_member(self, user_id):
        return self.member

    def get_card(self, card_id):
        return self.cards[0]

    def list_member_cards(self, user_id):
        return self.cards

    def get_club(self, club_id):
        return self.club


def test_verified_point_reads_preserve_raw_status_and_provenance():
    service = ArciPointReadService(SanitizedFixtureTransport())

    member = service.get_member("user-synthetic-001")
    card = service.get_card("card-synthetic-001")
    club = service.get_club("club-synthetic-001")

    assert member.source.locator == "/users/user-synthetic-001"
    assert card.status_raw == 20
    assert card.status_normalized is ArciCardStatus.APPROVED
    assert card.source.content_hash
    assert club.validity_year == 2026


def test_membership_verification_is_exact_and_fail_closed():
    transport = SanitizedFixtureTransport()
    service = ArciPointReadService(transport)

    eligible = service.verify_membership("user-synthetic-001", "club-synthetic-001")
    assert eligible.outcome is VerificationOutcome.VERIFIED_ELIGIBLE
    assert eligible.card_status == "APPROVED"
    assert len(eligible.evidence) == 3

    transport.cards[0]["expired"] = True
    ineligible = service.verify_membership("user-synthetic-001", "club-synthetic-001")
    assert ineligible.outcome is VerificationOutcome.VERIFIED_INELIGIBLE

    transport.cards[0]["user_id"] = "user-synthetic-002"
    unavailable = service.verify_membership("user-synthetic-001", "club-synthetic-001")
    assert unavailable.outcome is VerificationOutcome.SOURCE_UNAVAILABLE


def test_point_reads_reject_identity_mismatch_and_duplicate_cards():
    transport = SanitizedFixtureTransport()
    service = ArciPointReadService(transport)
    transport.member["id"] = "different-user"
    with pytest.raises(ArciPointReadError) as mismatch:
        service.get_member("user-synthetic-001")
    assert mismatch.value.code is ErrorCode.CONFLICT

    transport = SanitizedFixtureTransport()
    transport.cards.append(dict(transport.cards[0]))
    with pytest.raises(ArciPointReadError) as duplicate:
        ArciPointReadService(transport).list_member_cards("user-synthetic-001")
    assert duplicate.value.code is ErrorCode.CONFLICT


def test_mcp_exposes_only_injected_verified_reads_and_no_complete_lists():
    service = ArciPointReadService(SanitizedFixtureTransport())
    server = ArciMCPServer(FakeProvider(), service)
    names = {tool["name"] for tool in server.list_tools()}

    assert "arci_get_member" in names
    assert "arci_verify_membership" in names
    assert "arci_list_members" not in names
    assert "arci_list_cards" not in names
    assert "arci_list_pending_cards" not in names

    result = server.call("arci_verify_membership", {
        "user_id": "user-synthetic-001",
        "club_id": "club-synthetic-001",
    })
    assert result["structuredContent"]["data"]["outcome"] == "VERIFIED_ELIGIBLE"
    assert result["structuredContent"]["writes"] == 0

    denied = server.call("arci_get_member", {"user_id": "x", "url": "https://example.invalid"})
    assert denied["structuredContent"]["status"] == "POLICY_DENIED"
