from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from ralfloop_agent.unified_assistant.tiremm_admin import (
    Deadline, NextAction, Practice, PracticeArtifact, PracticeKind,
    PracticePriority, PracticeStatus, SourceRecord, SourcedFact, TiremmAdminStore,
)


FIXTURE = Path(__file__).parent / "fixtures" / "tiremm_admin_v0.json"
NOW = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)


def build_store() -> TiremmAdminStore:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    records = []
    aliases = {}
    for raw in payload["sources"]:
        key = raw.pop("key")
        record = SourceRecord.model_validate({"observed_at": NOW.isoformat(), **raw})
        records.append(record)
        aliases[key] = record.evidence_id
    store = TiremmAdminStore()
    store.ingest_snapshot(records)
    store.project(_practice(
        "ponti-culturali", PracticeKind.PROJECT, PracticeStatus.OPEN,
        PracticePriority.HIGH, NOW.replace(month=8, day=20),
        (aliases["bando_email"], aliases["bando_doc"]),
        deadlines=(Deadline(label="integrazione", due_at=NOW.replace(day=5), evidence_ids=(aliases["bando_email"], aliases["bando_doc"])),),
        next_action=NextAction(action_type="draft", description="Preparare integrazione", due_at=NOW.replace(day=4), requires_approval=False, evidence_ids=(aliases["bando_email"],)),
        documents=(PracticeArtifact(artifact_id="drive-doc-bando-01", label="Avviso ufficiale", evidence_ids=(aliases["bando_doc"],)),),
        communications=(PracticeArtifact(artifact_id="msg-bando-01", label="Richiesta integrazione", evidence_ids=(aliases["bando_email"],)),),
    ))
    store.project(_practice(
        "tari-sede", PracticeKind.MUNICIPAL, PracticeStatus.WAITING,
        PracticePriority.NORMAL, NOW.replace(month=7, day=1),
        (aliases["tari_pec"], aliases["tari_doc"]),
        next_action=NextAction(action_type="wait", description="Attendere risposta ufficio tributi", requires_approval=False, evidence_ids=(aliases["tari_pec"],)),
        documents=(PracticeArtifact(artifact_id="drive-tari-01", label="Planimetria", evidence_ids=(aliases["tari_doc"],)),),
    ))
    store.project(_practice(
        "rendiconto-giovani", PracticeKind.GRANT_REPORTING, PracticeStatus.BLOCKED,
        PracticePriority.URGENT, NOW.replace(month=8, day=26),
        (aliases["grant_email"], aliases["grant_doc"]),
        deadlines=(
            Deadline(label="rendiconto", due_at=NOW.replace(month=8, day=31), evidence_ids=(aliases["grant_email"],)),
            Deadline(label="rendiconto", due_at=NOW.replace(day=15), evidence_ids=(aliases["grant_doc"],)),
        ),
        blockers=(SourcedFact(field="missing_receipts", value="Ricevute mancanti", evidence_ids=(aliases["grant_email"],), inferred=True),),
        next_action=NextAction(action_type="collect", description="Raccogliere ricevute mancanti", requires_approval=False, blocked_by=("missing_receipts",), evidence_ids=(aliases["grant_email"],)),
    ))
    store.project(_practice(
        "famiglia-colloquio", PracticeKind.FAMILY_COMMUNICATION, PracticeStatus.OPEN,
        PracticePriority.NORMAL, NOW.replace(month=8, day=31),
        (aliases["family_email"], aliases["family_calendar"]),
        next_action=NextAction(action_type="draft", description="Preparare bozza con due slot", requires_approval=False, evidence_ids=(aliases["family_email"], aliases["family_calendar"])),
        communications=(PracticeArtifact(artifact_id="msg-family-01", label="Richiesta colloquio", evidence_ids=(aliases["family_email"],)),),
        events=(PracticeArtifact(artifact_id="event-family-01", label="Slot colloquio", evidence_ids=(aliases["family_calendar"],)),),
    ))
    store.project(_practice(
        "evento-quartiere", PracticeKind.EVENT, PracticeStatus.COMPLETED,
        PracticePriority.LOW, NOW.replace(month=8, day=15),
        (aliases["event_email"], aliases["event_calendar"]), next_action=None,
        events=(PracticeArtifact(artifact_id="event-event-01", label="Evento svolto", evidence_ids=(aliases["event_calendar"],)),),
    ))
    store.project(_practice(
        "arci-rinnovo", PracticeKind.ARCI_MEMBERSHIP, PracticeStatus.BLOCKED,
        PracticePriority.HIGH, NOW.replace(month=8, day=30),
        (aliases["arci_api"], aliases["arci_doc"]),
        blockers=(SourcedFact(field="signed_minutes", value="Verbale firmato mancante", evidence_ids=(aliases["arci_api"], aliases["arci_doc"])),),
        next_action=NextAction(action_type="collect", description="Ottenere verbale firmato", requires_approval=False, blocked_by=("signed_minutes",), evidence_ids=(aliases["arci_api"], aliases["arci_doc"])),
        documents=(PracticeArtifact(artifact_id="drive-arci-01", label="Verbale in bozza", evidence_ids=(aliases["arci_doc"],)),),
    ))
    return store


def _practice(
    identity, kind, status, priority, updated_at, evidence_ids, *, deadlines=(),
    blockers=(), next_action=None, communications=(), documents=(), events=(),
):
    return Practice(
        practice_id=f"practice.{identity}", title=identity.replace("-", " ").title(),
        kind=kind, status=status, priority=priority,
        opened_at=datetime(2026, 6, 1, tzinfo=timezone.utc), updated_at=updated_at,
        responsible_party="Tiremm Innanz APS", counterparties=("Controparte fixture",),
        evidence_ids=evidence_ids, deadlines=deadlines, blockers=blockers,
        next_action=next_action, communications=communications, documents=documents,
        events=events,
    )
