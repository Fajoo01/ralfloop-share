from __future__ import annotations

import json

from ralfloop_agent.unified_assistant.tuya_mcp import (
    TUYA_TOOLS,
    TuyaHARegistry,
    TuyaMCPServer,
)


class FakeBackend:
    def __init__(self) -> None:
        self.states = {
            "light.cucina": {"entity_id": "light.cucina", "state": "off", "attributes": {}},
            "sensor.disabled": {"entity_id": "sensor.disabled", "state": "12", "attributes": {}},
        }
        self.calls: list[tuple[str, str, dict]] = []

    def health(self):
        return {"status": "ok"}

    def list_entities(self):
        return tuple(self.states.values())

    def read_state(self, entity_id: str):
        return dict(self.states[entity_id])

    def call_service(self, service: str, entity_id: str, data: dict):
        self.calls.append((service, entity_id, dict(data)))
        if service == "turn_on":
            self.states[entity_id] = {**self.states[entity_id], "state": "on"}
        return {"ok": True}


def _write_storage(root, name: str, payload: dict) -> None:
    storage = root / ".storage"
    storage.mkdir(exist_ok=True)
    (storage / name).write_text(json.dumps(payload), encoding="utf-8")


def _registry(tmp_path):
    _write_storage(tmp_path, "core.config_entries", {
        "data": {"entries": [
            {"entry_id": "tuya-entry", "domain": "tuya"},
            {"entry_id": "mqtt-entry", "domain": "mqtt"},
        ]}
    })
    _write_storage(tmp_path, "core.entity_registry", {
        "data": {"entities": [
            {"entity_id": "light.cucina", "config_entry_id": "tuya-entry", "device_id": "dev1", "platform": "tuya", "original_name": "Cucina"},
            {"entity_id": "sensor.disabled", "config_entry_id": "tuya-entry", "device_id": "dev1", "platform": "tuya", "disabled_by": "user"},
            {"entity_id": "switch.altro", "config_entry_id": "mqtt-entry", "device_id": "dev2", "platform": "mqtt"},
        ]}
    })
    _write_storage(tmp_path, "core.device_registry", {
        "data": {"devices": [
            {"id": "dev1", "config_entries": ["tuya-entry"], "name": "Lampada Tuya", "manufacturer": "Tuya", "model": "L1"},
            {"id": "dev2", "config_entries": ["mqtt-entry"], "name": "Altro"},
        ]}
    })
    return TuyaHARegistry(tmp_path)


def test_tuya_server_exposes_only_expected_tools_and_owned_entities(tmp_path):
    backend = FakeBackend()
    server = TuyaMCPServer(registry=_registry(tmp_path), backend=backend)

    assert {row["name"] for row in server.list_tools()} == TUYA_TOOLS
    devices = server.call("tuya_list_devices", {})["structuredContent"]
    assert devices["count"] == 1
    assert devices["items"][0]["device_id"] == "dev1"
    assert devices["items"][0]["entity_count"] == 2

    entities = server.call("tuya_list_entities", {})["structuredContent"]
    assert [row["entity_id"] for row in entities["items"]] == ["light.cucina"]
    assert entities["items"][0]["state"]["state"] == "off"

    state = server.call("tuya_get_state", {"entity_id": "light.cucina"})["structuredContent"]
    assert state["state"]["state"] == "off"
    denied = server.call("tuya_get_state", {"entity_id": "switch.altro"})
    assert denied["isError"] is True
    assert denied["structuredContent"]["status"] == "tuya_entity_not_owned"


def test_tuya_service_allowlist_and_readback(tmp_path):
    backend = FakeBackend()
    server = TuyaMCPServer(registry=_registry(tmp_path), backend=backend)

    result = server.call("tuya_call_service", {
        "entity_id": "light.cucina", "service": "turn_on", "data": {},
    })
    payload = result["structuredContent"]
    assert payload["before"]["state"] == "off"
    assert payload["after"]["state"] == "on"
    assert payload["writes"] == 1
    assert backend.calls == [("turn_on", "light.cucina", {})]

    bad_service = server.call("tuya_call_service", {
        "entity_id": "light.cucina", "service": "unlock", "data": {},
    })
    assert bad_service["isError"] is True
    assert bad_service["structuredContent"]["status"] == "tuya_service_not_allowlisted"

    bad_data = server.call("tuya_call_service", {
        "entity_id": "light.cucina", "service": "turn_off", "data": {"transition": 1},
    })
    assert bad_data["isError"] is True
    assert bad_data["structuredContent"]["status"] == "tuya_service_data_not_allowlisted"
    assert len(backend.calls) == 1


def test_tuya_health_reports_owned_entity_availability(tmp_path):
    backend = FakeBackend()
    backend.states["light.cucina"] = {
        "entity_id": "light.cucina", "state": "unavailable", "attributes": {}
    }
    server = TuyaMCPServer(registry=_registry(tmp_path), backend=backend)

    payload = server.call("tuya_health", {})["structuredContent"]
    assert payload["status"] == "completed"
    assert payload["availability"] == "degraded"
    assert payload["state_health"]["unavailable"] == 1
    assert payload["state_health"]["missing"] == 0
    assert payload["state_health"]["unavailable_entities"][0]["entity_id"] == "light.cucina"
    assert payload["state_health"]["degraded_devices"][0]["device_id"] == "dev1"
    assert payload["writes"] == 0
    assert payload["sends"] == 0
