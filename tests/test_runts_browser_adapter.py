import json
from pathlib import Path

import pytest

from ralfloop_agent.unified_assistant.runts_browser_adapter import RuntsAuthenticatedBrowserAdapter, RuntsBrowserError


FIXTURE = json.loads((Path(__file__).parent / "fixtures/runts/read_surface.real_contract.sanitized.json").read_text())


class Transport:
    def __init__(self, practices=None, messages=None):
        self.payloads = {"practices": practices or (FIXTURE["practices"],), "messages": messages or (FIXTURE["messages"],)}
    def pages(self, kind): return self.payloads[kind]


def test_sanitized_observed_contract_maps_strict_dtos_with_provenance():
    adapter = RuntsAuthenticatedBrowserAdapter(Transport())
    practice = adapter.list_practices(limit=10)[0]
    message = adapter.list_messages("10001", limit=10)[0]
    assert message.practice_id == "10001"
    assert practice.native_id == "10001" and practice.status_raw == "SYNTHETIC_STATUS"
    assert message.native_id == "20001" and message.attachments[0].native_id == "30001"
    assert practice.source.content_hash and message.source.content_hash
    assert adapter.get_practice("10001") == practice
    assert adapter.get_message("20001") == message


def test_complete_enumeration_reconciles_total_and_deduplicates():
    first = {**FIXTURE["practices"], "totalElements": 2, "totalPages": 2}
    second = json.loads(json.dumps(first))
    second["listaIstanze"][0]["idIstanza"] = 10002
    adapter = RuntsAuthenticatedBrowserAdapter(Transport(practices=(first, second)))
    assert len(adapter.list_practices(limit=10)) == 2

    duplicate = RuntsAuthenticatedBrowserAdapter(Transport(practices=(first, first)))
    with pytest.raises(RuntsBrowserError, match="runts_duplicate_identity"):
        duplicate.list_practices(limit=10)


def test_incomplete_source_fails_closed():
    incomplete = {**FIXTURE["messages"], "totalElements": 2, "totalPages": 1}
    with pytest.raises(RuntsBrowserError, match="runts_incomplete_source"):
        RuntsAuthenticatedBrowserAdapter(Transport(messages=(incomplete,))).list_messages("10001", limit=10)


def test_missing_or_conflicting_scope_fails_closed_even_for_empty_result():
    adapter = RuntsAuthenticatedBrowserAdapter(Transport())
    with pytest.raises(RuntsBrowserError, match="scope_required"):
        adapter.list_messages(limit=10)
    with pytest.raises(RuntsBrowserError, match="scope_mismatch"):
        adapter.list_messages("10002")
    empty = {**FIXTURE["messages"], "totalElements":0,"totalPages":0,"listaMessaggi":[]}
    adapter = RuntsAuthenticatedBrowserAdapter(Transport(messages=(empty,)))
    assert adapter.list_messages("10001") == ()
    with pytest.raises(RuntsBrowserError, match="scope_mismatch"):
        adapter.list_messages("10002")


def test_nested_identity_cannot_disagree_with_authenticated_request_scope():
    page = json.loads(json.dumps(FIXTURE["messages"]))
    page["listaMessaggi"][0]["istanza"] = {"idIstanza": 10002}
    with pytest.raises(RuntsBrowserError, match="scope_mismatch"):
        RuntsAuthenticatedBrowserAdapter(Transport(messages=(page,))).list_messages("10001")


def test_metadata_hash_is_not_document_binary_hash():
    message = RuntsAuthenticatedBrowserAdapter(Transport()).list_messages("10001")[0]
    assert message.attachments[0].content_hash is None
    assert message.attachments[0].source.content_hash
