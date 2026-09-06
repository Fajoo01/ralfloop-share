from ralfloop_agent.unified_assistant.pec_browser_adapter import PecAuthenticatedBrowserAdapter, PecBrowserError


def test_cdp_call_preserves_interleaved_network_events():
    import json
    from ralfloop_agent.unified_assistant.pec_browser_adapter import PecAuthenticatedCdpTransport

    event = {"method": "Network.requestWillBeSent", "params": {"requestId": "synthetic"}}
    class Socket:
        def __init__(self): self.rows = iter((event, {"id": 1, "result": {}}))
        def send(self, value): pass
        def recv(self): return json.dumps(next(self.rows))
    transport = PecAuthenticatedCdpTransport()
    transport._client = Socket()
    assert transport._call(1, "Network.enable", {}) == {}
    assert list(transport._events) == [event]


def test_pec_auth_errors_are_not_generic_outages():
    assert PecBrowserError("pec_auth_required").status == "AUTH_REQUIRED"
    assert PecBrowserError("pec_session_expired").status == "SESSION_EXPIRED"
    assert PecBrowserError("pec_page_timeout").status == "SOURCE_UNAVAILABLE"
    assert PecBrowserError("pec_incomplete_source").status == "INCOMPLETE_SOURCE"


def payload(page, total, ids):
    return {
        "pageInfo": {"page": page, "pageCount": 2, "itemCount": total},
        "data": [{
            "objectId": identity, "subject": f"RUNTS pratica n. {identity}",
            "email": "synthetic@example.invalid", "rawdate": "2026-09-03T08:00:00Z",
            "text": "Synthetic notification", "isUnread": True,
            "isCertificataMessage": True, "attach": [],
        } for identity in ids],
    }


class Transport:
    def __init__(self, pages): self.pages, self.index = pages, 0
    def first_page(self): self.index = 0; return self.pages[0]
    def next_page(self): self.index += 1; return self.pages[self.index] if self.index < len(self.pages) else None


def test_complete_pagination_and_exact_cached_read():
    adapter = PecAuthenticatedBrowserAdapter(Transport([payload(1, 3, ["p-1", "p-2"]), payload(2, 3, ["p-3"])]))
    rows = adapter.list_messages(limit=3)
    assert [row.native_id for row in rows] == ["p-1", "p-2", "p-3"]
    assert rows[0].runts_reference == "p-1"
    assert rows[0].source.content_hash
    assert adapter.get_message("p-3").native_id == "p-3"


def test_incomplete_and_repeated_pages_fail_closed():
    adapter = PecAuthenticatedBrowserAdapter(Transport([payload(1, 3, ["p-1"])]))
    try:
        adapter.list_messages(limit=3)
        assert False
    except PecBrowserError as exc:
        assert str(exc) == "pec_incomplete_source"

    adapter = PecAuthenticatedBrowserAdapter(Transport([payload(1, 2, ["p-1"]), payload(1, 2, ["p-2"])]))
    try:
        adapter.list_messages(limit=2)
        assert False
    except PecBrowserError as exc:
        assert str(exc) == "pec_repeated_page"


def test_duplicate_native_ids_fail_closed():
    adapter = PecAuthenticatedBrowserAdapter(Transport([payload(1, 2, ["p-1", "p-1"])]))
    try:
        adapter.list_messages(limit=2)
        assert False
    except PecBrowserError as exc:
        assert str(exc) == "pec_duplicate_message_id"


def test_reference_search_enumerates_past_result_limit_and_matches_exactly():
    # MOCK: the match is beyond the former 100-message retrieval window.
    first = payload(1, 102, [f"other-{i}" for i in range(100)])
    last = payload(2, 102, ["26039420", "2603942"])
    transport = Transport([first, last])
    adapter = PecAuthenticatedBrowserAdapter(transport)
    rows = adapter.find_by_runts_reference("2603942", limit=1)
    assert [row.native_id for row in rows] == ["2603942"]
    assert transport.index == 1
    assert rows[0].source.content_hash


def test_reference_search_never_reports_absence_from_incomplete_source():
    import pytest
    adapter = PecAuthenticatedBrowserAdapter(Transport([payload(1, 101, [f"p-{i}" for i in range(100)])]))
    with pytest.raises(PecBrowserError, match="pec_incomplete_source"):
        adapter.find_by_runts_reference("2603942", limit=100)


def test_reference_search_fails_closed_when_matches_exceed_limit():
    import pytest
    page = payload(1, 2, ["p-1", "p-2"])
    for row in page["data"]:
        row["subject"] = "RUNTS pratica n. 2603942"
    adapter = PecAuthenticatedBrowserAdapter(Transport([page]))
    with pytest.raises(PecBrowserError, match="pec_incomplete_source"):
        adapter.find_by_runts_reference("2603942", limit=1)
