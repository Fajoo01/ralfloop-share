from __future__ import annotations

from pathlib import Path

import pytest

from ralfloop_agent.unified_assistant.home import HomeEntityRegistry, HomeIntentParser, HomeWorkflow
from ralfloop_agent.unified_assistant.home_provider import (
    HomeAssistantProviderError,
    HomeAssistantRESTBackend,
)
from ralfloop_agent.unified_assistant.registry import DEFAULT_HOME_ENTITIES


class FakeHA:
    def __init__(self):
        self.state = {"entity_id": "light.striscia_cucina", "state": "off", "attributes": {"friendly_name": "Striscia cucina"}}
        self.calls = []

    def __call__(self, method, path, payload):
        self.calls.append((method, path, payload))
        if path == "/api/":
            return {"message": "API running"}
        if path == "/api/states":
            return [self.state]
        if path == "/api/states/light.striscia_cucina":
            return self.state
        if path == "/api/services/light/turn_on":
            self.state = {**self.state, "state": "on"}
            return [self.state]
        raise AssertionError(path)


def test_real_inventory_contains_only_observed_ids_and_protected_gate():
    registry = HomeEntityRegistry.load(DEFAULT_HOME_ENTITIES)

    assert registry.by_id["light.striscia_cucina"].auto_write
    assert registry.by_id["climate.maria_tinello"].maximum == 32
    assert registry.by_id["switch.cancello_switch_1"].protected
    assert "light.kitchen" not in registry.by_id


def test_rest_provider_read_write_readback_and_allowlist():
    fake = FakeHA()
    backend = HomeAssistantRESTBackend("http://ha.local", "secret-not-output", requester=fake)
    entity = HomeEntityRegistry.load(DEFAULT_HOME_ENTITIES).by_id["light.striscia_cucina"]
    workflow = HomeWorkflow(HomeEntityRegistry((entity,)), backend)

    result = workflow.execute(workflow.prepare(HomeIntentParser().parse("Accendi luce cucina")))

    assert result.status == "verified"
    assert result.write_calls == 1
    assert [row[0] for row in fake.calls] == ["GET", "POST", "GET"]
    assert "secret-not-output" not in repr(result)
    with pytest.raises(HomeAssistantProviderError, match="home_service_not_allowlisted"):
        backend.call_service("unlock", "lock.front", {})
    with pytest.raises(HomeAssistantProviderError, match="home_entity_invalid"):
        backend.call_service("turn_on", "all", {})


def test_environment_file_loads_without_exposing_token(tmp_path):
    env_file = tmp_path / "home.env"
    env_file.write_text("HA_URL=http://ha.local\nHA_TOKEN=top-secret\n", encoding="utf-8")
    backend = HomeAssistantRESTBackend.from_environment(env_file=env_file)

    assert backend.base_url == "http://ha.local"
    assert "top-secret" not in repr({"base_url": backend.base_url})
