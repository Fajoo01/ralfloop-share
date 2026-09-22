from __future__ import annotations

import json
import sys

import pytest

from tools.abc_relation_record_events import (
    load_events,
    main,
    normalize_event,
    proposal_digest,
    require_confirmed_digest,
)


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


def test_proposal_digest_is_stable_for_equivalent_normalized_events():
    first = normalize_event(_event())
    second_input = dict(reversed(list(_event().items())))
    second = normalize_event(second_input)
    assert proposal_digest([first]) == proposal_digest([second])


def test_confirmed_digest_rejects_missing_or_changed_proposal():
    event = normalize_event(_event())
    digest = proposal_digest([event])
    assert require_confirmed_digest([event], digest) == digest

    with pytest.raises(ValueError, match="requires --confirm-digest"):
        require_confirmed_digest([event], None)

    changed = dict(event)
    changed["summary"] = "Changed after preview."
    with pytest.raises(ValueError, match="does not match"):
        require_confirmed_digest([changed], digest)


def test_main_dry_run_emits_proposal_digest(tmp_path, monkeypatch, capsys):
    path = tmp_path / "events.json"
    path.write_text(json.dumps([_event()]), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["abc_relation_record_events.py", str(path)])
    assert main() == 0
    output = json.loads(capsys.readouterr().out)
    assert output["mode"] == "dry_run"
    assert output["proposal_digest"] == proposal_digest(load_events(path))


def test_main_commit_requires_matching_digest_before_write(tmp_path, monkeypatch):
    path = tmp_path / "events.json"
    path.write_text(json.dumps([_event()]), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["abc_relation_record_events.py", str(path), "--commit"])
    with pytest.raises(SystemExit) as missing:
        main()
    assert missing.value.code == 2

    monkeypatch.setattr(
        sys,
        "argv",
        ["abc_relation_record_events.py", str(path), "--commit", "--confirm-digest", "0" * 64],
    )
    with pytest.raises(SystemExit) as mismatch:
        main()
    assert mismatch.value.code == 2
