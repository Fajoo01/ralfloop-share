from __future__ import annotations

import pytest

from ralfloop_agent.unified_assistant.arci_cdp_transport import ArciAuthenticatedCdpTransport
from ralfloop_agent.unified_assistant.arci_datatables import ArciListQuery
from ralfloop_agent.unified_assistant.arci_point_reads import ArciPointReadError
from ralfloop_agent.unified_assistant.browser_read_only import FixedScriptResult
from ralfloop_agent.unified_assistant.platform import ErrorCode


class FixedClient:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def call_fixed_function(self, **kwargs):
        self.calls.append(kwargs)
        return FixedScriptResult(value=self.value, operation=kwargs["operation"])


def test_authenticated_transport_uses_fixed_semantic_operation_only():
    client = FixedClient({"data": {"id": "user-1"}})
    transport = ArciAuthenticatedCdpTransport(client)

    assert transport.get_member("user-1") == {"id": "user-1"}
    call = client.calls[0]
    assert call["expected_host"] == "webapp.tessera-arci.it"
    assert call["arguments"] == ("get_member", {"user_id": "user-1"})
    assert "token" not in repr(call["arguments"]).casefold()


def test_datatables_transport_emits_typed_query_without_url_argument():
    client = FixedClient({"data": {"data": [], "meta": {"current_page": 2, "last_page": 2, "total": 0}}})
    transport = ArciAuthenticatedCdpTransport(client)
    query = ArciListQuery(club_id="club-1", validity=2026)

    transport.list_cards_page(query, 2)

    operation, payload = client.calls[0]["arguments"]
    assert operation == "list_cards_page"
    assert payload["page"] == 2
    assert payload["club_id"] == "club-1"
    assert "url" not in payload


def test_transport_normalizes_error_without_leaking_body():
    transport = ArciAuthenticatedCdpTransport(FixedClient({"transport_error": "UNAUTHORIZED"}))
    with pytest.raises(ArciPointReadError) as exc:
        transport.get_card("card-1")
    assert exc.value.code is ErrorCode.UNAUTHORIZED
    assert str(exc.value) == "arci_read_failed"
