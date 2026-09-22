from __future__ import annotations

import json

from tools.abc_relation_propose import classify_text, propose_event
from tools.abc_relation_record_events import load_events, proposal_digest


def test_natural_fact_defaults_to_observed_fact():
    kind, confidence, tags, reason = classify_text("oggi abbiamo cenato insieme")
    assert kind == "observed_fact"
    assert confidence == 0.9
    assert "auto_classified" in tags
    assert reason == "default_observable"


def test_explicit_refusal_is_boundary():
    kind, confidence, tags, reason = classify_text(
        "oggi mi ha detto che preferisce non uscire da sola con me"
    )
    assert kind == "boundary"
    assert confidence == 0.9
    assert "explicit_boundary" in tags
    assert reason == "explicit_boundary_marker"


def test_interpretive_language_is_inference_and_requires_review():
    event, review = propose_event(
        "oggi mi sembra gelosa",
        occurred_at="2026-09-21T21:30:00+02:00",
        actor="Arianna",
    )
    assert event["kind"] == "inference"
    assert event["confidence"] == 0.45
    assert event["summary"] == "mi sembra gelosa"
    assert review["requires_human_review"] is True


def test_plain_absence_is_not_misclassified_as_boundary():
    kind, _, _, _ = classify_text("oggi non mi ha scritto")
    assert kind == "observed_fact"


def test_proposal_wrapper_round_trips_into_guarded_recorder(tmp_path):
    event, review = propose_event(
        "oggi abbiamo preso un caffè insieme",
        occurred_at="2026-09-21T10:00:00+02:00",
        source_ref="project_chat:test",
    )
    events = [event]
    payload = {
        "events": events,
        "proposal_digest": proposal_digest(events),
        "review": review,
    }
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    loaded = load_events(path)
    assert loaded == events
    assert proposal_digest(loaded) == payload["proposal_digest"]
