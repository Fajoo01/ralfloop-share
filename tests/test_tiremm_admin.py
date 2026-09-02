from datetime import datetime, timezone

import pytest

from ralfloop_agent.unified_assistant.contracts import MemoryNamespace
from ralfloop_agent.unified_assistant.tiremm_admin import (
    Deadline,
    NextAction,
    Practice,
    PracticeKind,
    PracticePriority,
    PracticeStatus,
    SourceKind,
    SourceRecord,
    TiremmAdminEvalCase,
    TiremmAdminStore,
    evaluate_admin,
)


NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)


def source(kind: SourceKind, identity: str, content: str) -> SourceRecord:
    return SourceRecord(
        source_kind=kind, source_id=identity, observed_at=NOW,
        title=identity, content=content, location=f"fixture:{identity}",
        metadata={"trust": "authenticated", "freshness": "current"},
    )


def practice(*evidence_ids: str, deadlines=(), status=PracticeStatus.OPEN) -> Practice:
    return Practice(
        practice_id="practice.ponti-culturali",
        title="Ponti culturali urbani",
        kind=PracticeKind.PROJECT,
        status=status,
        priority=PracticePriority.HIGH,
        opened_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        responsible_party="Tiremm Innanz APS",
        counterparties=("Comune di Milano",),
        evidence_ids=evidence_ids,
        deadlines=deadlines,
        next_action=None if status is PracticeStatus.COMPLETED else NextAction(
            action_type="draft",
            description="Preparare la bozza, senza inviarla",
            requires_approval=True,
            due_at=datetime(2026, 9, 20, tzinfo=timezone.utc),
            evidence_ids=(evidence_ids[0],),
        ),
        updated_at=NOW,
    )


def test_snapshot_ingestion_is_deduplicated_and_bounded():
    record = source(SourceKind.GMAIL, "m-1", "Scadenza 30 settembre")
    store = TiremmAdminStore(max_sources=1)
    assert len(store.ingest_snapshot((record,))) == 1
    assert store.ingest_snapshot((record,)) == ()
    assert store.metrics()["tiremm_admin_duplicates"] == 1
    with pytest.raises(ValueError, match="source_quota"):
        store.ingest_snapshot((source(SourceKind.PEC, "pec-2", "Seconda fonte"),))


def test_projection_fails_closed_when_evidence_is_missing_or_stale():
    record = source(SourceKind.DOCUMENT, "doc-1", "Verbale")
    store = TiremmAdminStore()
    with pytest.raises(ValueError, match="evidence_missing"):
        store.project(practice(record.evidence_id))
    store.ingest_snapshot((record,))
    current = practice(record.evidence_id)
    store.project(current)
    stale = current.model_copy(update={"updated_at": datetime(2026, 9, 1, tzinfo=timezone.utc)})
    with pytest.raises(ValueError, match="projection_stale"):
        store.project(stale)


def test_conflicting_deadlines_are_retained_not_silently_resolved():
    email = source(SourceKind.GMAIL, "m-1", "Scadenza 30 settembre")
    document = source(SourceKind.DOCUMENT, "d-1", "Scadenza 2 ottobre")
    store = TiremmAdminStore()
    store.ingest_snapshot((email, document))
    store.project(practice(
        email.evidence_id, document.evidence_id,
        deadlines=(
            Deadline(label="invio domanda", due_at=datetime(2026, 9, 30, tzinfo=timezone.utc), evidence_ids=(email.evidence_id,)),
            Deadline(label="Invio domanda", due_at=datetime(2026, 10, 2, tzinfo=timezone.utc), evidence_ids=(document.evidence_id,)),
        ),
    ))
    hit = store.retrieve("Ponti scadenza domanda")[0]
    assert len(hit.conflicts) == 1
    assert hit.conflicts[0].values == (
        "2026-09-30T00:00:00+00:00", "2026-10-02T00:00:00+00:00",
    )


def test_closed_practice_cannot_expose_next_action():
    record = source(SourceKind.ARCI, "arci-1", "Pratica conclusa")
    with pytest.raises(ValueError, match="closed_practice"):
        Practice(
            practice_id="practice.closed", title="Conclusa",
            status=PracticeStatus.COMPLETED, evidence_ids=(record.evidence_id,),
            kind=PracticeKind.EVENT, opened_at=NOW,
            responsible_party="Tiremm Innanz APS",
            next_action=NextAction(
                action_type="submit", description="Invia", requires_approval=True,
                evidence_ids=(record.evidence_id,),
            ), updated_at=NOW,
        )


def test_retrieval_memory_projection_and_admin_eval_are_source_bound():
    email = source(SourceKind.GMAIL, "m-1", "Bozza richiesta dal Comune")
    calendar = source(SourceKind.CALENDAR, "e-1", "Promemoria 20 settembre")
    store = TiremmAdminStore()
    store.ingest_snapshot((email, calendar))
    store.project(practice(email.evidence_id, calendar.evidence_id))
    results = evaluate_admin(store, (
        TiremmAdminEvalCase(
            case_id="find-next-action", query="Ponti culturali prossima bozza",
            expected_practice_ids=("practice.ponti-culturali",),
            expected_requires_approval=True,
        ),
        TiremmAdminEvalCase(
            case_id="unknown-practice", query="pratica inesistente zeta",
            expected_practice_ids=(),
        ),
    ))
    assert all(result.passed for result in results)
    items = store.memory_items()
    assert len(items) == 1
    assert items[0].namespace is MemoryNamespace.TIREMM
    assert set(items[0].source_refs) == {email.evidence_id, calendar.evidence_id}


def test_practice_rejects_evidence_not_declared_at_top_level():
    first = source(SourceKind.DOCUMENT, "d-1", "Documento")
    second = source(SourceKind.GMAIL, "m-2", "Email")
    with pytest.raises(ValueError, match="undeclared_evidence"):
        practice(
            first.evidence_id,
            deadlines=(Deadline(
                label="scadenza", due_at=NOW, evidence_ids=(second.evidence_id,),
            ),),
        )
