from __future__ import annotations

from ralfloop_agent.unified_assistant.meteo_mcp_adapter import (
    _extract_address,
)
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from ralfloop_agent.unified_assistant.runtime import unified_route_probe
from ralfloop_agent.unified_assistant.telegram_location import (
    load_telegram_location,
    remember_telegram_location,
)


def test_meteo_registered_and_read_only():
    registry = UnifiedRegistryFacade()

    skill = registry.skill("meteo.read")
    assert skill.classification == "READ"
    assert set(skill.required_capabilities) == {
        "meteo.current",
        "meteo.radar",
        "meteo.geocode",
    }

    tools = {item.id: item for item in registry.list_tools()}
    tool = tools["meteo.radar.mcp"]
    assert tool.classification == "READ"
    assert tool.side_effect_class == "none"


def test_planner_routes_weather_before_home():
    registry = UnifiedRegistryFacade()
    planner = UnifiedPlanner(registry)

    for text in (
        "meteo via Padova 100 Milano",
        "piove a Milano?",
        "fammi vedere il radar qui",
        "temperatura a Milano",
    ):
        plan = planner.validate(planner.plan(text))
        assert plan.assignments[0].skill == "meteo.read"


def test_address_extraction():
    assert _extract_address("meteo via Padova 100 Milano") == (
        "via Padova 100 Milano"
    )
    assert _extract_address("che tempo fa in Milano?") == "Milano"
    assert _extract_address("piove a Sesto San Giovanni?") == (
        "Sesto San Giovanni"
    )
    assert _extract_address("radar qui") is None


def test_short_lived_telegram_location(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "RALFLOOP_TELEGRAM_LOCATION_DIR",
        str(tmp_path),
    )
    monkeypatch.setenv(
        "RALFLOOP_TELEGRAM_LOCATION_TTL_SEC",
        "7200",
    )

    msg = {
        "chat": {"id": 123},
        "from": {"id": 456},
        "location": {
            "latitude": 45.4639102,
            "longitude": 9.1906398,
        },
    }

    assert remember_telegram_location(msg)

    found = load_telegram_location(123, 456)
    assert found is not None
    assert found["lat"] == 45.4639102
    assert found["lon"] == 9.1906398


def test_route_probe_exposes_meteo_connector(monkeypatch):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")

    result = unified_route_probe(
        "meteo via Padova 100 Milano",
        {"source": "ralf_terminal"},
    )

    assert result is not None
    assert result["skills_used"] == ["meteo.read"]
    assert result["write_policy"] == "no_write"
    assert result["requires_confirmation"] is False
    assert "meteo.radar.mcp" in result["mcp_connectors"]
