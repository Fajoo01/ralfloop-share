from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ThermostatReading:
    current_temperature_c: float | None
    target_temperature_c: float | None
    hvac_state: str | None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_ha_thermostat_state(
    state: Mapping[str, Any], policy: Mapping[str, Any]
) -> ThermostatReading:
    """Decode the known BHT/MOES half-degree protocol exposed wrongly by HA.

    The Tuya cloud metadata for product 4jdveazecxcrdbgq declares scale=1, so HA
    exposes 54 as 5.4 C. The physical device uses half-degree raw units: 54=27 C.
    Since HA has already divided by ten, the correction at this boundary is x5.
    """
    cfg = policy["boiler_thermostat"]
    multiplier = float(cfg["ha_to_physical_multiplier"])
    bias = float(cfg.get("calibration_bias_c", 0.0))
    current = _number(state.get("current_temperature"))
    target = _number(state.get("temperature"))
    return ThermostatReading(
        current_temperature_c=None if current is None else current * multiplier + bias,
        target_temperature_c=None if target is None else target * multiplier,
        hvac_state=None if state.get("state") is None else str(state.get("state")),
    )


def physical_target_to_ha(temperature_c: float, policy: Mapping[str, Any]) -> float:
    cfg = policy["boiler_thermostat"]
    value = float(temperature_c)
    minimum = float(cfg["min_temperature_c"])
    maximum = float(cfg["max_temperature_c"])
    step = float(cfg["step_c"])
    if value < minimum or value > maximum:
        raise ValueError("boiler_target_out_of_range")
    steps = round((value - minimum) / step)
    snapped = minimum + steps * step
    if abs(snapped - value) > 1e-9:
        raise ValueError("boiler_target_not_on_step")
    return round(value / float(cfg["ha_to_physical_multiplier"]), 3)
