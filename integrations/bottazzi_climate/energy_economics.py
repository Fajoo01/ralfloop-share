from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, date
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from src.mcp_transport import MCPClientSession, UnixMCPTransport


class EnergyEconomicsError(RuntimeError):
    pass


@dataclass(frozen=True)
class EnergyEconomicsContext:
    status: str
    meter_total_kwh: float | None
    current_power_kw: float | None
    air_conditioner_state: str | None
    tariff_estimated_consumed_kwh: float | None
    tariff_remaining_kwh: float | None
    electricity_marginal_eur_per_kwh: float | None
    gas_useful_eur_per_kwh: float | None
    estimated_cop: float | None
    heatpump_useful_eur_per_kwh: float | None
    break_even_cop: float | None
    preferred_heating_source: str | None
    relative_saving: float | None
    cop_source: str | None
    price_band: str | None
    bill_stale_days: int | None
    writes: int
    sends: int
    accounting_source: str | None


def _structured(result: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = result.get("structuredContent")
    if not isinstance(payload, Mapping) or payload.get("ok") is False:
        raise EnergyEconomicsError("tuya_energy_read_failed")
    return payload


def _read_entity(session: MCPClientSession, entity_id: str) -> Mapping[str, Any]:
    payload = _structured(session.call_tool("tuya_get_state", {"entity_id": entity_id}))
    if int(payload.get("writes") or 0) or int(payload.get("sends") or 0):
        raise EnergyEconomicsError("unexpected_energy_read_side_effect")
    return payload


def _float_state(payload: Mapping[str, Any]) -> float:
    state = payload.get("state")
    if not isinstance(state, Mapping):
        raise EnergyEconomicsError("energy_state_missing")
    try:
        return float(state.get("state"))
    except (TypeError, ValueError) as exc:
        raise EnergyEconomicsError("energy_state_not_numeric") from exc


def _entity_meta(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    entity = payload.get("entity")
    if not isinstance(entity, Mapping):
        raise EnergyEconomicsError("energy_entity_missing")
    return entity


def _interpolate(points: Sequence[Sequence[float]], x: float) -> float:
    rows = sorted((float(a), float(b)) for a, b in points)
    if not rows:
        raise EnergyEconomicsError("cop_curve_missing")
    if x <= rows[0][0]:
        return rows[0][1]
    if x >= rows[-1][0]:
        return rows[-1][1]
    for (x0, y0), (x1, y1) in zip(rows, rows[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * ((x - x0) / (x1 - x0))
    return rows[-1][1]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(dict(value), handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _state_path(policy: Mapping[str, Any]) -> Path:
    runtime = policy.get("calendar_runtime") or {}
    configured = str(runtime.get("state_dir") or "~/.local/share/bottazzi/sede-climate")
    directory = Path(os.getenv("RALF_SEDE_CLIMATE_STATE_DIR", configured)).expanduser()
    name = str((policy.get("energy_economics") or {}).get("state_file") or "energy-state.json")
    return directory / name


def _bill_anchor(cfg: Mapping[str, Any], now: datetime) -> tuple[float, int]:
    consumed = float(cfg["latest_bill_consumed_with_losses_kwh"])
    annual = float(cfg.get("historical_annual_kwh") or 0.0)
    bill_end = date.fromisoformat(str(cfg["latest_bill_period_end"]))
    stale_days = max((now.date() - bill_end).days, 0)
    estimated_gap = max(annual, 0.0) / 365.0 * stale_days
    return consumed + estimated_gap, stale_days


def _tariff_accounting(
    cfg: Mapping[str, Any], now: datetime, meter_kwh: float,
    previous: Mapping[str, Any], price_source: str,
) -> tuple[float, float, dict[str, Any], int, str]:
    anchor_estimate, stale_days = _bill_anchor(cfg, now)
    same_source = previous.get("price_source") == price_source
    old_meter = previous.get("meter_anchor_kwh")
    old_consumed = previous.get("tariff_consumed_at_anchor_kwh")
    if same_source and old_meter is not None and old_consumed is not None:
        anchor_meter = float(old_meter)
        anchor_consumed = float(old_consumed)
        delta = meter_kwh - anchor_meter
        if delta < -0.01:
            anchor_meter = meter_kwh
            anchor_consumed = max(float(previous.get("last_tariff_consumed_kwh") or anchor_consumed), anchor_estimate)
            delta = 0.0
            source = "bill_estimate_plus_meter_after_reset"
        else:
            source = "bill_estimate_plus_live_meter_delta"
    else:
        anchor_meter = meter_kwh
        anchor_consumed = anchor_estimate
        delta = 0.0
        source = "bill_plus_historical_gap_estimate_anchor"
    consumed = max(anchor_consumed + max(delta, 0.0), 0.0)
    included = float(cfg["included_kwh"])
    remaining = max(included - consumed, 0.0)
    state = {
        "schema_version": 1,
        "price_source": price_source,
        "meter_anchor_kwh": anchor_meter,
        "tariff_consumed_at_anchor_kwh": anchor_consumed,
        "last_meter_kwh": meter_kwh,
        "last_tariff_consumed_kwh": consumed,
        "updated_at": now.isoformat(),
    }
    return consumed, remaining, state, stale_days, source


def _price_band(cfg: Mapping[str, Any], remaining_kwh: float) -> tuple[str, float]:
    if remaining_kwh > 0.0:
        return "included_fixed", float(cfg["marginal_eur_per_kwh_below_threshold"])
    return "indexed_reference", float(cfg["marginal_eur_per_kwh_above_threshold_reference"])


def _choose_source(
    gas_cost: float, heatpump_cost: float, margin: float,
) -> tuple[str, float]:
    if heatpump_cost <= gas_cost * (1.0 - margin):
        return "heat_pump", max((gas_cost - heatpump_cost) / gas_cost, 0.0)
    if gas_cost <= heatpump_cost * (1.0 - margin):
        return "boiler", max((heatpump_cost - gas_cost) / heatpump_cost, 0.0)
    cheaper = min(gas_cost, heatpump_cost)
    dearer = max(gas_cost, heatpump_cost)
    return "economic_tie", 0.0 if dearer <= 0 else (dearer - cheaper) / dearer


def fetch_energy_economics(
    policy: Mapping[str, Any], *, now: datetime,
    outdoor_temperature_c: float | None,
    persist: bool = True,
) -> EnergyEconomicsContext:
    cfg = policy.get("energy_economics")
    if not isinstance(cfg, Mapping) or not cfg.get("enabled", False):
        return EnergyEconomicsContext("disabled", None, None, None, None, None, None, None, None, None, None, None, None, None, None, 0, 0, None)
    elec = cfg["electricity"]
    gas = cfg["gas"]
    hp = cfg["heat_pump"]
    tuya = policy.get("tuya_read") or {}
    session = MCPClientSession(
        UnixMCPTransport(str(tuya.get("socket_path") or "/run/ralf-tuya-mcp/mcp.sock")),
        timeout=float(tuya.get("timeout_seconds", 20.0)),
        client_name="bottazzi-sede-energy-read",
    )
    try:
        session.initialize(); tools = {tool.name for tool in session.list_tools()}
        if "tuya_get_state" not in tools:
            raise EnergyEconomicsError("tuya_get_state_not_discovered")
        meter_payload = _read_entity(session, str(elec["meter_entity_id"]))
        meta = _entity_meta(meter_payload)
        if str(meta.get("device_id") or "") != str(elec["meter_device_id"]):
            raise EnergyEconomicsError("energy_meter_device_mismatch")
        if str(meta.get("site") or "") != str(elec.get("meter_site") or ""):
            raise EnergyEconomicsError("energy_meter_site_mismatch")
        meter_kwh = _float_state(meter_payload)
        powers = [_float_state(_read_entity(session, str(entity))) for entity in elec.get("power_entity_ids", ())]
        ac_payload = _read_entity(session, str(hp["entity_id"]))
        ac_state = str((ac_payload.get("state") or {}).get("state") or "")
    finally:
        session.close()

    path = _state_path(policy)
    previous = _load_state(path) if persist else {}
    price_source = str(elec.get("price_source") or "")
    consumed, remaining, next_state, stale_days, accounting_source = _tariff_accounting(
        elec, now, meter_kwh, previous, price_source,
    )
    if persist:
        _write_state(path, next_state)
    band, electricity_price = _price_band(elec, remaining)
    gas_kwh_per_smc = float(gas["pcs_kwh_per_smc"]) * float(gas["boiler_efficiency"])
    gas_cost = float(gas["marginal_eur_per_smc"]) / gas_kwh_per_smc
    cop = None if outdoor_temperature_c is None else _interpolate(hp["cop_curve"], float(outdoor_temperature_c))
    hp_cost = None if cop is None or cop <= 0 else electricity_price / cop
    break_even = electricity_price / gas_cost if gas_cost > 0 else None
    preferred = None
    relative = None
    if hp_cost is not None:
        preferred, relative = _choose_source(
            gas_cost, hp_cost,
            float((cfg.get("decision") or {}).get("minimum_relative_saving", 0.05)),
        )
    return EnergyEconomicsContext(
        status="ok",
        meter_total_kwh=meter_kwh,
        current_power_kw=sum(powers) if powers else None,
        air_conditioner_state=ac_state,
        tariff_estimated_consumed_kwh=consumed,
        tariff_remaining_kwh=remaining,
        electricity_marginal_eur_per_kwh=electricity_price,
        gas_useful_eur_per_kwh=gas_cost,
        estimated_cop=cop,
        heatpump_useful_eur_per_kwh=hp_cost,
        break_even_cop=break_even,
        preferred_heating_source=preferred,
        relative_saving=relative,
        cop_source=str(hp.get("cop_curve_source") or "configured_curve"),
        price_band=band,
        bill_stale_days=stale_days,
        writes=0,
        sends=0,
        accounting_source=accounting_source,
    )


def unavailable_energy_context(status: str) -> EnergyEconomicsContext:
    return EnergyEconomicsContext(
        status=status, meter_total_kwh=None, current_power_kw=None,
        air_conditioner_state=None, tariff_estimated_consumed_kwh=None,
        tariff_remaining_kwh=None, electricity_marginal_eur_per_kwh=None,
        gas_useful_eur_per_kwh=None, estimated_cop=None,
        heatpump_useful_eur_per_kwh=None, break_even_cop=None,
        preferred_heating_source=None, relative_saving=None,
        cop_source=None, price_band=None, bill_stale_days=None,
        writes=0, sends=0, accounting_source=None,
    )
