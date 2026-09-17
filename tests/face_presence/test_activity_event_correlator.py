import json
from pathlib import Path

from integrations.bottazzi_presence.activity_event_correlator import normalize, run


def test_normalize_strips_biometric_fields():
    row = {"event": "presence_inferred", "ts": "2026-09-16T10:00:00", "name": "fabio", "embedding": [1, 2], "frame": "/secret.jpg"}
    out = normalize("presence", row)
    assert out is not None
    assert out["payload"]["name"] == "fabio"
    assert "embedding" not in out["payload"]
    assert "frame" not in out["payload"]


def test_first_run_is_baseline_and_second_run_emits_only_new(tmp_path: Path):
    src = tmp_path / "garden.jsonl"
    src.write_text(json.dumps({"event": "human_passage", "event_id": "old", "ts": "2026-09-16T10:00:00"}) + "\n")
    state = tmp_path / "state.json"
    out = tmp_path / "out.jsonl"
    sources = {"garden": src}
    first = run(sources, state, out)
    assert first["status"] == "baseline_initialized"
    assert not out.exists()
    with src.open("a") as fh:
        fh.write(json.dumps({"event": "human_passage", "event_id": "new", "ts": "2026-09-16T10:01:00"}) + "\n")
    second = run(sources, state, out)
    assert second["emitted"] == 1
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]["source_id"] == "new"


def test_domotics_transition_is_normalized():
    row = {"event": "DOMOTICS_ENTITY_RECOVERED", "entity_id": "switch.test", "ts": "2026-09-16T10:02:00"}
    out = normalize("domotics", row)
    assert out is not None
    assert out["event_type"] == "DOMOTICS_ENTITY_RECOVERED"
    assert out["payload"]["entity_id"] == "switch.test"


def test_truncated_source_does_not_duplicate_seen_event(tmp_path: Path):
    src = tmp_path / "garden.jsonl"
    state = tmp_path / "state.json"
    out = tmp_path / "out.jsonl"
    sources = {"garden": src}
    src.write_text(json.dumps({"event": "human_passage", "event_id": "old", "ts": "2026-09-16T10:00:00"}) + "\n")
    run(sources, state, out)
    with src.open("a") as fh:
        fh.write(json.dumps({"event": "human_passage", "event_id": "new", "ts": "2026-09-16T10:01:00"}) + "\n")
    assert run(sources, state, out)["emitted"] == 1
    # Simulate log rotation/truncation containing an already-seen event plus a new one.
    src.write_text(
        json.dumps({"event": "human_passage", "event_id": "new", "ts": "2026-09-16T10:01:00"}) + "\n" +
        json.dumps({"event": "human_passage", "event_id": "newer", "ts": "2026-09-16T10:02:00"}) + "\n"
    )
    result = run(sources, state, out)
    assert result["emitted"] == 1
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert [r["source_id"] for r in rows] == ["new", "newer"]


def test_sede_sources_get_explicit_site():
    out = normalize("garden", {"event": "human_passage", "event_id": "g1", "ts": "2026-09-17T10:00:00"})
    assert out is not None
    assert out["site"] == "sede"
    assert out["payload"]["site"] == "sede"


def test_ha_sede_state_change_normalizes_read_only_fields():
    out = normalize("ha_sede", {
        "event": "HA_STATE_CHANGED", "event_id": "h1", "ts": "2026-09-17T10:00:00",
        "site": "sede", "entity_id": "switch.cancello_switch_1", "domain": "switch",
        "old_state": "off", "new_state": "on", "attributes": {"secret": "x"},
    })
    assert out is not None
    assert out["site"] == "sede"
    assert out["payload"]["old_state"] == "off"
    assert out["payload"]["new_state"] == "on"
    assert "attributes" not in out["payload"]
