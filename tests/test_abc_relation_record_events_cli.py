from __future__ import annotations

import json

import pytest

from tools.abc_relation_record_events import load_events, normalize_event


def _event() -> dict[str, object]:
    return {
        "occurred_at": "2026-09-21T08:00:00+02:00",
        "kind": "observed_fact",
        "summary": "  Concrete event.  ",
        "source_kind": "chatgpt_snapshot",
        "source_ref": "project_chat:test",
        "confidence": 0.9,
        "weight": 4,
        "tags": [" shared_time ", ""],
    }


def test_normalize_event_keeps_only_structured_evidence():
    row = normalize_event(_event())
    assert row["summary"] == "Concrete event."
    assert row["tags"] == ["shared_time"]
    assert row["confidence"] == 0.9
    assert row["weight"] == 4.0


def test_normalize_event_rejects_naive_time_and_unknown_fields():
    row = _event()
    row["occurred_at"] = "2026-09-21T08:00:00"
    with pytest.raises(ValueError, match="timezone"):
        normalize_event(row)

    row = _event()
    row["unexpected"] = True
    with pytest.raises(ValueError, match="unknown fields"):
        normalize_event(row)


def test_load_events_accepts_events_wrapper(tmp_path):
    path = tmp_path / "events.json"
    path.write_text(json.dumps({"events": [_event()]}), encoding="utf-8")
    rows = load_events(path)
    assert len(rows) == 1
    assert rows[0]["source_kind"] == "chatgpt_snapshot"


def test_raw_excerpt_cap_is_enforced():
    row = _event()
    row["raw_excerpt"] = "x" * 601
    with pytest.raises(ValueError, match="600"):
        normalize_event(row)


def test_load_events_rejects_non_object_rows(tmp_path):
    path = tmp_path / "events.json"
    path.write_text(json.dumps([_event(), "bad"]), encoding="utf-8")
    with pytest.raises(ValueError, match="every event"):
        load_events(path)
