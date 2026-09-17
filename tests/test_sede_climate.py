from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from integrations.bottazzi_climate.comfort import select_seasonal_comfort
from integrations.bottazzi_climate.planner import CalendarEvent, plan_event, plan_event_with_weather
from integrations.bottazzi_climate import meteo as climate_meteo
from integrations.bottazzi_climate.meteo import parse_meteo_context
from integrations.bottazzi_climate.thermostat import (
    normalize_ha_thermostat_state,
    physical_target_to_ha,
)


def policy():
    return json.loads(Path("config/sede_climate_policy.json").read_text())


def test_bht_half_degree_protocol_is_normalized():
    reading = normalize_ha_thermostat_state(
        {"state": "heat_cool", "current_temperature": 5.4, "temperature": 3.8},
        policy(),
    )
    assert reading.current_temperature_c == 27.0
    assert reading.target_temperature_c == 19.0


def test_physical_target_encodes_back_to_ha_buggy_scale():
    assert physical_target_to_ha(20.0, policy()) == 4.0
    assert physical_target_to_ha(19.5, policy()) == 3.9


def test_target_range_and_step_are_guarded():
    with pytest.raises(ValueError, match="out_of_range"):
        physical_target_to_ha(4.5, policy())
    with pytest.raises(ValueError, match="not_on_step"):
        physical_target_to_ha(20.2, policy())


def test_online_calendar_event_is_excluded():
    p=policy(); now=datetime(2026,9,17,12,0)
    e=CalendarEvent("x","Tedesco (online)",now+timedelta(hours=4),now+timedelta(hours=5),calendar_id=p["calendar"]["calendar_id"])
    assert plan_event(e,now=now,current_temperature_c=27.0,policy=p)["decision"] == "ignore"


def test_in_person_sede_event_uses_corrected_temperature():
    p=policy(); now=datetime(2026,9,17,12,0)
    e=CalendarEvent("x","Tedesco",now+timedelta(hours=3),now+timedelta(hours=4),calendar_id=p["calendar"]["calendar_id"])
    result=plan_event(e,now=now,current_temperature_c=27.1,policy=p,boiler_available=True)
    assert result["decision"] == "precondition"
    assert result["mode"] == "cool"
    assert result["actuator"] == "climate.air_conditioner"



def test_september_uses_shoulder_profile_with_wider_band():
    p=policy(); now=datetime(2026,9,17,12,0)
    e=CalendarEvent(
        "x", "Riunione Tiremm", now+timedelta(hours=3), now+timedelta(hours=4),
        location="Via Privata Federico Jarach, 8", calendar_id=p["calendar"]["calendar_id"],
    )
    expected = {
        18.4: ("precondition", "heat"),
        18.5: ("no_climate_action", None),
        27.0: ("no_climate_action", None),
        27.1: ("precondition", "cool"),
    }
    for temp, (decision, mode) in expected.items():
        result=plan_event(e,now=now,current_temperature_c=temp,policy=p,boiler_available=True)
        assert result["comfort_profile"] == "shoulder"
        assert result["decision"] == decision
        assert result.get("mode") == mode


def test_running_mean_overrides_calendar_season():
    p=policy(); now=datetime(2026,1,17,12,0)
    winter=select_seasonal_comfort(now=now,policy=p,outdoor_recent_mean_c=10.0)
    summer=select_seasonal_comfort(now=now,policy=p,outdoor_recent_mean_c=24.0)
    assert (winter.name, winter.source) == ("winter", "outdoor_recent_mean")
    assert winter.heating_enabled is True and winter.cooling_enabled is False
    assert (summer.name, summer.source) == ("summer", "outdoor_recent_mean")
    assert summer.heating_enabled is False and summer.cooling_enabled is True


def test_summer_disables_heating_and_winter_disables_cooling():
    p=policy()
    summer_now=datetime(2026,7,17,12,0)
    winter_now=datetime(2026,1,17,12,0)
    summer_event=CalendarEvent("s","Estate",summer_now+timedelta(hours=3),summer_now+timedelta(hours=4),calendar_id=p["calendar"]["calendar_id"])
    winter_event=CalendarEvent("w","Inverno",winter_now+timedelta(hours=3),winter_now+timedelta(hours=4),calendar_id=p["calendar"]["calendar_id"])
    assert plan_event(summer_event,now=summer_now,current_temperature_c=16.0,policy=p)["decision"] == "no_climate_action"
    assert plan_event(winter_event,now=winter_now,current_temperature_c=30.0,policy=p)["decision"] == "no_climate_action"

def test_negative_deadband_is_treated_as_zero():
    p=policy(); p["seasonal_comfort"]["enabled"]=False; p["comfort"]["deadband_c"]=-1.0; now=datetime(2026,9,17,12,0)
    e=CalendarEvent(
        "x", "Riunione Tiremm", now+timedelta(hours=3), now+timedelta(hours=4),
        location="Via Privata Federico Jarach, 8", calendar_id=p["calendar"]["calendar_id"],
    )
    assert plan_event(e,now=now,current_temperature_c=18.9,policy=p)["mode"] == "heat"
    assert plan_event(e,now=now,current_temperature_c=26.1,policy=p)["mode"] == "cool"

def test_site_map_keeps_boiler_only_in_sede():
    sites=json.loads(Path("config/tuya_sites.json").read_text())["sites"]
    dev=policy()["boiler_thermostat"]["device_id"]
    assert dev in sites["sede"]["device_ids"]
    assert dev not in sites["camper"]["device_ids"]
    assert dev not in sites["asiago"]["device_ids"]


def test_meteo_recent_mean_requires_enough_history():
    good = parse_meteo_context({
        "structuredContent": {
            "recent_temperature_mean_c": 21.7,
            "recent_temperature_hours": 168,
        }
    }, min_recent_hours=72)
    short = parse_meteo_context({
        "recent_temperature_mean_c": 12.0,
        "recent_temperature_hours": 12,
    }, min_recent_hours=72)
    assert (good.recent_mean_c, good.status) == (21.7, "ok")
    assert (short.recent_mean_c, short.status) == (None, "insufficient_history")


def test_live_meteo_mean_shape_selects_weather_profile_without_network():
    p=policy(); now=datetime(2026,9,17,12,0)
    context=parse_meteo_context({
        "recent_temperature_mean_c": 21.77,
        "recent_temperature_hours": 168,
    })
    comfort=select_seasonal_comfort(
        now=now, policy=p, outdoor_recent_mean_c=context.recent_mean_c
    )
    assert (comfort.name, comfort.source) == ("shoulder", "outdoor_recent_mean")


def test_fetch_outdoor_weather_reads_configured_meteo_mcp(monkeypatch):
    p=policy()
    seen={}
    def fake_rpc(sock_path, address, timeout_s, lat=None, lon=None):
        seen.update(socket_path=sock_path, address=address, timeout=timeout_s, lat=lat, lon=lon)
        return {
            "structuredContent": {
                "recent_temperature_mean_c": 14.2,
                "recent_temperature_hours": 168,
            }
        }
    monkeypatch.setattr(climate_meteo, "_rpc", fake_rpc)
    context=climate_meteo.fetch_outdoor_weather(p)
    assert (context.recent_mean_c, context.history_hours, context.status) == (14.2, 168, "ok")
    assert seen["socket_path"] == p["weather"]["socket_path"]
    assert seen["address"] == p["weather"]["address"]
    assert seen["lat"] == p["weather"]["lat"]
    assert seen["lon"] == p["weather"]["lon"]

def test_weather_mean_22_49_keeps_september_in_shoulder():
    p=policy(); now=datetime(2026,9,17,15,0)
    comfort=select_seasonal_comfort(now=now,policy=p,outdoor_recent_mean_c=22.49)
    assert (comfort.name, comfort.source) == ("shoulder", "outdoor_recent_mean")


def test_plan_event_with_weather_wires_meteo_into_dry_run(monkeypatch):
    p=policy(); now=datetime(2026,9,17,15,0)
    e=CalendarEvent(
        "live", "Riunione Tiremm", now+timedelta(hours=2), now+timedelta(hours=3),
        location="Via Privata Federico Jarach, 8", calendar_id=p["calendar"]["calendar_id"],
    )
    monkeypatch.setattr(
        climate_meteo, "fetch_outdoor_weather",
        lambda _p: climate_meteo.OutdoorWeatherContext(22.49, 168, "ok"),
    )
    result=plan_event_with_weather(
        e, now=now, current_temperature_c=23.0, policy=p, boiler_available=True
    )
    assert result["dry_run"] is True
    assert result["comfort_profile"] == "shoulder"
    assert result["comfort_profile_source"] == "outdoor_recent_mean"
    assert result["weather_status"] == "ok"
    assert result["weather_history_hours"] == 168
    assert result["decision"] == "no_climate_action"
