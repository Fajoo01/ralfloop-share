from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from integrations.bottazzi_climate.planner import CalendarEvent, plan_event
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
    result=plan_event(e,now=now,current_temperature_c=27.0,policy=p,boiler_available=True)
    assert result["decision"] == "precondition"
    assert result["mode"] == "cool"
    assert result["actuator"] == "climate.air_conditioner"


def test_site_map_keeps_boiler_only_in_sede():
    sites=json.loads(Path("config/tuya_sites.json").read_text())["sites"]
    dev=policy()["boiler_thermostat"]["device_id"]
    assert dev in sites["sede"]["device_ids"]
    assert dev not in sites["camper"]["device_ids"]
    assert dev not in sites["asiago"]["device_ids"]
