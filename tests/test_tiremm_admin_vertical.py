from __future__ import annotations

from datetime import timedelta
import sqlite3

import pytest
from pydantic import ValidationError

from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags
from ralfloop_agent.unified_assistant.tiremm_admin import (
    ActionProposal, PracticeStatus, SourceKind, TiremmAdminQueryAdapter, TiremmAdminSQLite,
    TiremmIngestionPipeline, reject_external_execution,
)
from tiremm_admin_fixture import NOW, build_store


EVAL_CASES = (
    ("open_count", lambda s: len(s.list_open_practices()) == 5),
    ("done_excluded_from_open", lambda s: s.get_practice("practice.evento-quartiere") not in s.list_open_practices()),
    ("blocked_count", lambda s: len(s.blocked()) == 2),
    ("blocked_arci", lambda s: s.get_practice("practice.arci-rinnovo") in s.blocked()),
    ("waiting_tari", lambda s: s.waiting()[0].practice_id == "practice.tari-sede"),
    ("overdue_reporting", lambda s: s.get_practice("practice.rendiconto-giovani") in s.overdue(NOW)),
    ("imminent_project", lambda s: s.get_practice("practice.ponti-culturali") in s.due_within(NOW, timedelta(days=7))),
    ("done_not_overdue", lambda s: s.get_practice("practice.evento-quartiere") not in s.overdue(NOW)),
    ("conflict_detected", lambda s: len(s.get_conflicts("practice.rendiconto-giovani")) == 1),
    ("conflict_unresolved", lambda s: s.get_conflicts("practice.rendiconto-giovani")[0].resolution_status == "unresolved"),
    ("conflict_keeps_both", lambda s: len(s.get_conflicts("practice.rendiconto-giovani")[0].values) == 2),
    ("conflict_needs_verification", lambda s: s.get_practice("practice.rendiconto-giovani").needs_verification),
    ("next_action_arci", lambda s: s.get_practice("practice.arci-rinnovo").next_action.action_type == "collect"),
    ("next_action_family", lambda s: s.get_practice("practice.famiglia-colloquio").next_action.action_type == "draft"),
    ("document_retrieval", lambda s: s.retrieve("Ponti avviso")[0].practice.documents[0].artifact_id == "drive-doc-bando-01"),
    ("communication_retrieval", lambda s: s.retrieve("famiglia colloquio")[0].practice.communications[0].artifact_id == "msg-family-01"),
    ("source_provenance", lambda s: len(s.get_sources("practice.tari-sede")) == 2),
    ("source_ids_real", lambda s: all(row.evidence_id.startswith("src.") for row in s.get_sources("practice.tari-sede"))),
    ("latest_verified", lambda s: s.latest_verified_update("practice.ponti-culturali").source_id == "msg-bando-01"),
    ("unknown_empty", lambda s: s.retrieve("pratica totalmente inesistente zeta") == ()),
    ("stale_tari", lambda s: s.get_practice("practice.tari-sede") in s.stale_since(NOW, timedelta(days=30))),
    ("not_stale_family", lambda s: s.get_practice("practice.famiglia-colloquio") not in s.stale_since(NOW, timedelta(days=30))),
    ("blocker_source_bound", lambda s: bool(s.get_practice("practice.arci-rinnovo").blockers[0].evidence_ids)),
    ("no_next_action_empty", lambda s: s.no_next_action() == ()),
    ("six_fixture_practices", lambda s: s.metrics()["tiremm_admin_practices"] == 6),
)


@pytest.mark.parametrize(("case_id", "check"), EVAL_CASES, ids=[row[0] for row in EVAL_CASES])
def test_tiremm_admin_eval_v0(case_id, check):
    assert check(build_store()), case_id


def test_ingestion_incremental_cursor_and_changed_content():
    store = build_store()
    pipeline = TiremmIngestionPipeline(store)
    raw = {
        "source_kind": "mailchimp", "source_id": "campaign-1",
        "observed_at": NOW.isoformat(), "source_timestamp": NOW.isoformat(),
        "title": "Newsletter", "content": "Draft one", "location": "fixture:mailchimp/campaign-1",
        "metadata": {"trust": "authenticated"}, "cursor": "cursor-10",
    }
    first = pipeline.ingest(raw)
    second = pipeline.ingest(raw)
    changed = pipeline.ingest({**raw, "content": "Draft two", "cursor": "cursor-11"})
    assert first.changed_since_last_ingestion
    assert not second.changed_since_last_ingestion
    assert changed.changed_since_last_ingestion
    assert pipeline.cursors[SourceKind.MAILCHIMP] == "cursor-11"
    assert not first.persistence_allowed


def test_sqlite_roundtrip_and_schema_version(tmp_path):
    original = build_store()
    database = TiremmAdminSQLite(tmp_path / "admin.sqlite3")
    for practice in original.list_open_practices():
        for source in original.get_sources(practice.practice_id):
            record = original.source_record(source.evidence_id)
            database.persist_source(record, last_seen=NOW)
        database.persist_practice(practice)
    completed = original.get_practice("practice.evento-quartiere")
    for source in original.get_sources(completed.practice_id):
        database.persist_source(original.source_record(source.evidence_id), last_seen=NOW)
    database.persist_practice(completed)
    restored = database.load()
    database.close()
    assert restored.metrics()["tiremm_admin_practices"] == 6
    assert restored.get_practice("practice.ponti-culturali") == original.get_practice("practice.ponti-culturali")


def test_schema_migration_fails_closed(tmp_path):
    path = tmp_path / "admin.sqlite3"
    database = TiremmAdminSQLite(path)
    database.close()
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
    connection.commit(); connection.close()
    with pytest.raises(ValueError, match="migration_required"):
        TiremmAdminSQLite(path)


def test_malformed_source_rejected():
    pipeline = TiremmIngestionPipeline(build_store())
    with pytest.raises(ValidationError):
        pipeline.ingest({"source_kind": "gmail", "source_id": "missing-fields"})


def test_state_transition_requires_valid_edge_and_evidence():
    store = build_store()
    practice = store.get_practice("practice.ponti-culturali")
    evidence = practice.evidence_ids[0]
    completed = store.transition(
        practice.practice_id, PracticeStatus.COMPLETED,
        evidence_ids=(evidence,), updated_at=NOW,
    )
    assert completed.status is PracticeStatus.COMPLETED
    assert completed.next_action is None
    with pytest.raises(ValueError, match="transition_invalid"):
        store.transition(
            practice.practice_id, PracticeStatus.OPEN,
            evidence_ids=(evidence,), updated_at=NOW,
        )
    with pytest.raises(ValueError, match="transition_evidence_missing"):
        store.transition(
            practice.practice_id, PracticeStatus.ARCHIVED,
            evidence_ids=("src.000000000000000000000000",), updated_at=NOW,
        )


def test_action_proposal_can_be_drafted_but_never_executed():
    store = build_store()
    evidence = store.get_sources("practice.famiglia-colloquio")[0]
    proposal = ActionProposal(
        action="draft_email", target="family-fixture@example.invalid",
        payload={"subject": "Proposta colloquio"}, evidence_ids=(evidence.evidence_id,),
        reason="Requested by source-backed practice",
    )
    assert store.validate_action_proposal(proposal) is proposal
    assert proposal.requires_approval
    with pytest.raises(PermissionError, match="execute_disabled"):
        reject_external_execution(proposal)
    with pytest.raises(ValidationError):
        ActionProposal.model_validate({**proposal.model_dump(), "requires_approval": False})
    invented = proposal.model_copy(update={"evidence_ids": ("src.000000000000000000000000",)})
    with pytest.raises(ValueError, match="evidence_missing"):
        store.validate_action_proposal(invented)


def test_llm_adapter_retrieves_minimal_source_backed_context():
    context = TiremmAdminQueryAdapter(build_store()).context("Quali pratiche sono bloccate?", now=NOW)
    assert context.query_type == "blocked"
    assert len(context.practices) == 2
    assert context.verified
    assert context.validation_error is None
    assert all(source.evidence_id.startswith("src.") for source in context.sources)


def test_llm_adapter_fails_closed_for_unknown_request():
    context = TiremmAdminQueryAdapter(build_store()).context("informazione senza riscontro zeta", now=NOW)
    assert not context.verified
    assert context.validation_error == "source_backed_information_missing"


def test_feature_flag_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RALFLOOP_TIREMM_ADMIN", raising=False)
    assert not AssistantFeatureFlags.from_env().tiremm_admin
    monkeypatch.setenv("RALFLOOP_TIREMM_ADMIN", "1")
    assert AssistantFeatureFlags.from_env().tiremm_admin
