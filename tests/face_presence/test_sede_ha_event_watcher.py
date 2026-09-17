import json
from pathlib import Path

from scripts.bottazzi_sede_ha_event_watcher import load_config, sanitize_state_event, websocket_url


def _msg(entity_id="binary_sensor.sensore_bagno_tiremm_occupazione", old="off", new="on"):
    return {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": "2026-09-17T10:00:00+00:00",
            "data": {
                "entity_id": entity_id,
                "old_state": {"state": old, "attributes": {"secret": "x"}},
                "new_state": {"state": new, "attributes": {"secret": "y"}},
            },
        },
    }


def test_load_config_and_ws_url(tmp_path: Path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"site": "sede", "entities": ["binary_sensor.x"]}))
    assert load_config(p) == ("sede", {"binary_sensor.x": "primary_observation"})
    assert websocket_url("http://127.0.0.1:8123") == "ws://127.0.0.1:8123/api/websocket"
    assert websocket_url("https://ha.example") == "wss://ha.example/api/websocket"


def test_sanitize_allowlisted_state_change_only():
    allowed = {"binary_sensor.sensore_bagno_tiremm_occupazione": "primary_observation"}
    out = sanitize_state_event(_msg(), site="sede", allowed=allowed)
    assert out is not None
    assert out["event"] == "HA_STATE_CHANGED"
    assert out["site"] == "sede"
    assert out["old_state"] == "off" and out["new_state"] == "on"
    assert "attributes" not in out
    assert sanitize_state_event(_msg(entity_id="switch.other"), site="sede", allowed=allowed) is None
    assert sanitize_state_event(_msg(old="on", new="on"), site="sede", allowed=allowed) is None


def test_auxiliary_relay_role_is_preserved():
    allowed = {"switch.cancello_switch_1": "auxiliary_relay"}
    out = sanitize_state_event(_msg(entity_id="switch.cancello_switch_1"), site="sede", allowed=allowed)
    assert out is not None
    assert out["signal_role"] == "auxiliary_relay"
