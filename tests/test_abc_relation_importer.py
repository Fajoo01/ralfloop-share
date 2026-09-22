from __future__ import annotations

import json

from ralfloop_agent.abc_relation.importer import LEGACY_WARNING, import_legacy_bundle
from ralfloop_agent.abc_relation.service import RelationService
from ralfloop_agent.abc_relation.store import RelationStore


def test_import_legacy_bundle_creates_snapshot_without_turning_probs_into_facts(tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "context": {"status_summary": "fase favorevole ma non definita"},
        "current_state": {
            "relationship_shape": "quasi-coppia implicita",
            "main_dynamic": "avvicinamento con regolazione",
        },
    }), encoding="utf-8")
    probability = tmp_path / "prob.json"
    probability.write_text(json.dumps({
        "probabilities": {"quasi_couple_implicit": 0.65, "current_clear_choice": 0.25},
    }), encoding="utf-8")
    strategy = tmp_path / "strategy.json"
    strategy.write_text(json.dumps({
        "strategy": {
            "current_mode": "calore alto, iniziativa selettiva, zero fame",
            "do": ["creare momenti semplici"],
            "dont": ["non interrogare"],
        }
    }), encoding="utf-8")
    timeline = tmp_path / "timeline.json"
    timeline.write_text(json.dumps({
        "recent_timeline": [{"event": "cena condivisa", "meaning": "ipotesi di vicinanza"}],
    }), encoding="utf-8")

    service = RelationService(RelationStore(tmp_path / "abc.sqlite3"))
    result = import_legacy_bundle(service, [state, probability, strategy, timeline])

    assert result["imported_files"] == 4
    snapshot = service.store.latest_snapshot()
    assert snapshot is not None
    assert LEGACY_WARNING in snapshot.warnings
    assert "quasi-coppia implicita" in snapshot.state_summary
    assert all(row.status == "weak" for row in snapshot.hypotheses)
    assert all(row.confidence <= 0.35 for row in snapshot.hypotheses)
    assert snapshot.legacy_timeline[0]["event"] == "cena condivisa"
    assert snapshot.legacy_timeline[0]["status"] == "weak_historical_note"
    assert service.store.counts()["observed_fact"] == 0
    assert service.store.counts()["inference"] == 0


def test_missing_files_are_skipped_but_reported(tmp_path):
    valid = tmp_path / "state.json"
    valid.write_text(json.dumps({"context": {"status_summary": "state"}}), encoding="utf-8")
    service = RelationService(RelationStore(tmp_path / "abc.sqlite3"))

    result = import_legacy_bundle(service, [valid, tmp_path / "missing.json"])

    assert result["imported_files"] == 1
    assert result["skipped_files"] == 1
    assert any(row.startswith("missing:") for row in result["warnings"])
