from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from integrations.bottazzi_climate.calendar_mcp import CalendarReadResult
from integrations.bottazzi_climate.energy_economics import (
    EnergyEconomicsContext,
    _interpolate,
    _load_price_snapshot,
    _price_band,
    _tariff_accounting,
)
from integrations.bottazzi_climate.meteo import OutdoorWeatherContext, parse_meteo_context
from integrations.bottazzi_climate.planner import CalendarEvent
from integrations.bottazzi_climate.runtime import run_calendar_climate_cycle
from integrations.bottazzi_climate.thermostat import ThermostatReading
from integrations.bottazzi_climate.tuya_read import IndoorClimateContext

TZ = ZoneInfo("Europe/Rome")


def policy() -> dict:
    return json.loads(Path("config/sede_climate_policy.json").read_text())


def energy(source: str, cop: float, hp_cost: float, gas_cost: float = 0.104) -> EnergyEconomicsContext:
    return EnergyEconomicsContext(
        status="ok", meter_total_kwh=62.6, current_power_kw=0.003,
        air_conditioner_state="off", tariff_estimated_consumed_kwh=211.5,
        tariff_remaining_kwh=1288.5, electricity_marginal_eur_per_kwh=0.2765,
        gas_useful_eur_per_kwh=gas_cost, estimated_cop=cop,
        heatpump_useful_eur_per_kwh=hp_cost, break_even_cop=2.66,
        preferred_heating_source=source, relative_saving=0.25,
        cop_source="test_curve", price_band="included_fixed",
        bill_stale_days=48, writes=0, sends=0, accounting_source="test",
        electricity_price_source="test", gas_price_source="test",
        price_snapshot_status="fresh", price_snapshot_age_hours=1.0,
    )


def indoor(temp: float) -> IndoorClimateContext:
    return IndoorClimateContext(
        reading=ThermostatReading(temp, 19.0, "heat_cool"), status="ok",
        entity_id="climate.termostato", site="sede", writes=0, sends=0,
        raw_state={"current_temperature": temp / 5.0, "temperature": 3.8},
    )


class FakeCalendarReader:
    def __init__(self, event: CalendarEvent, now: datetime) -> None:
        self.event = event
        self.now = now

    def list_upcoming(self, _now: datetime) -> CalendarReadResult:
        return CalendarReadResult(
            events=(self.event,), window_start=self.now,
            window_end=self.now + timedelta(days=14),
            listing_complete=True, listed_count=1,
        )


def test_meteo_context_keeps_current_temperature_for_cop():
    ctx = parse_meteo_context({
        "structuredContent": {
            "recent_temperature_mean_c": 10.0,
            "recent_temperature_hours": 168,
            "current": {"temperature_2m": 5.5},
        }
    })
    assert ctx.status == "ok"
    assert ctx.current_c == 5.5
    assert ctx.recent_mean_c == 10.0


def test_tariff_accounting_anchors_meter_then_uses_real_delta():
    cfg = policy()["energy_economics"]["electricity"]
    now = datetime(2026, 9, 17, 16, 45, tzinfo=TZ)
    consumed1, remaining1, state, _, source1 = _tariff_accounting(
        cfg, now, 62.6, {}, cfg["price_source"]
    )
    consumed2, remaining2, _, _, source2 = _tariff_accounting(
        cfg, now + timedelta(days=1), 67.1, state, cfg["price_source"]
    )
    assert source1 == "bill_plus_historical_gap_estimate_anchor"
    assert source2 == "bill_estimate_plus_live_meter_delta"
    assert round(consumed2 - consumed1, 3) == 4.5
    assert round(remaining1 - remaining2, 3) == 4.5


def test_mistral_cop_curve_interpolates_configured_points():
    curve = policy()["energy_economics"]["heat_pump"]["cop_curve"]
    assert _interpolate(curve, 7.0) == 4.0
    assert 3.7 < _interpolate(curve, 5.0) < 3.8


def test_runtime_selects_heat_pump_when_economics_prefers_it(tmp_path):
    p = policy(); p["calendar_runtime"]["state_dir"] = str(tmp_path)
    now = datetime(2026, 1, 17, 12, 0, tzinfo=TZ)
    event = CalendarEvent(
        "heat", "Riunione Tiremm", now + timedelta(hours=2),
        now + timedelta(hours=3), calendar_id=p["calendar"]["calendar_id"],
    )
    result = run_calendar_climate_cycle(
        p, now=now, calendar_reader=FakeCalendarReader(event, now),
        indoor_fetch=lambda _: indoor(17.0),
        weather_fetch=lambda _: OutdoorWeatherContext(8.0, 168, "ok", 7.0),
        energy_fetch=lambda *args, **kwargs: energy("heat_pump", 4.0, 0.069),
    )
    plan = result["blocks"][0]
    assert plan["actuator"] == "climate.air_conditioner"
    assert plan["heating_source"] == "heat_pump"
    assert plan["economics_applied"] is True
    assert plan["estimated_cop"] == 4.0
    assert result["climate_writes"] == 0 and result["climate_sends"] == 0


def test_runtime_keeps_boiler_when_economics_prefers_gas(tmp_path):
    p = policy(); p["calendar_runtime"]["state_dir"] = str(tmp_path)
    now = datetime(2026, 1, 17, 12, 0, tzinfo=TZ)
    event = CalendarEvent(
        "heat", "Riunione Tiremm", now + timedelta(hours=2),
        now + timedelta(hours=3), calendar_id=p["calendar"]["calendar_id"],
    )
    result = run_calendar_climate_cycle(
        p, now=now, calendar_reader=FakeCalendarReader(event, now),
        indoor_fetch=lambda _: indoor(17.0),
        weather_fetch=lambda _: OutdoorWeatherContext(3.0, 168, "ok", -3.0),
        energy_fetch=lambda *args, **kwargs: energy("boiler", 2.7, 0.14),
    )
    plan = result["blocks"][0]
    assert plan["actuator"] == "climate.termostato"
    assert plan["heating_source"] == "boiler"
    assert plan["economics_preferred_heating_source"] == "boiler"
    assert result["energy_economics"]["preferred_heating_source"] == "boiler"


def test_fresh_price_snapshot_overrides_bill_baseline(tmp_path):
    p = policy(); cfg = p["energy_economics"]
    snap = tmp_path / "prices.json"
    cfg["price_snapshot"] = {"path": str(snap), "max_age_hours": 48}
    now = datetime(2026, 9, 17, 16, 45, tzinfo=TZ)
    snap.write_text(json.dumps({
        "schema_version": 1, "observed_at": now.isoformat(),
        "source": "test-live-prices",
        "electricity_marginal_eur_per_kwh_below_threshold": 0.25,
        "gas_marginal_eur_per_smc": 1.20,
    }))
    value, status, age = _load_price_snapshot(cfg, now)
    band, price, source = _price_band(cfg["electricity"], 100.0, value)
    assert status == "fresh" and age == 0.0
    assert band == "included_fixed" and price == 0.25
    assert source == "test-live-prices"
    assert value["gas_marginal_eur_per_smc"] == 1.20


def test_stale_price_snapshot_falls_back_to_verified_bill(tmp_path):
    p = policy(); cfg = p["energy_economics"]
    snap = tmp_path / "prices.json"
    cfg["price_snapshot"] = {"path": str(snap), "max_age_hours": 24}
    now = datetime(2026, 9, 17, 16, 45, tzinfo=TZ)
    snap.write_text(json.dumps({
        "schema_version": 1,
        "observed_at": (now - timedelta(hours=30)).isoformat(),
        "source": "stale-test",
        "electricity_marginal_eur_per_kwh_below_threshold": 0.01,
    }))
    value, status, age = _load_price_snapshot(cfg, now)
    band, price, source = _price_band(cfg["electricity"], 100.0, value)
    assert status == "stale" and age > 24
    assert value == {}
    assert band == "included_fixed"
    assert price == cfg["electricity"]["marginal_eur_per_kwh_below_threshold"]
    assert source == cfg["electricity"]["price_source"]


def test_sede_policy_disables_wrong_site_live_meter():
    elec = policy()["energy_economics"]["electricity"]
    assert elec["live_meter_enabled"] is False
    assert elec["live_meter_status"] == "disabled_wrong_site_asiago"
    assert "meter_entity_id" not in elec
    assert "meter_device_id" not in elec


def test_tariff_accounting_without_sede_meter_uses_bill_estimate_only():
    cfg = policy()["energy_economics"]["electricity"]
    now = datetime(2026, 9, 17, 17, 50, tzinfo=TZ)
    consumed, remaining, state, _, source = _tariff_accounting(
        cfg, now, None, {"meter_anchor_kwh": 62.6}, cfg["price_source"]
    )
    assert source == "bill_plus_historical_gap_estimate_no_sede_meter"
    assert state["live_meter"] == "disabled"
    assert "meter_anchor_kwh" not in state
    assert consumed > 0 and remaining < cfg["included_kwh"]
