from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping


@dataclass(frozen=True)
class SeasonalComfort:
    name: str
    source: str
    heating_enabled: bool
    cooling_enabled: bool
    heat_below_c: float
    heat_target_c: float
    cool_above_c: float
    cool_target_c: float
    deadband_c: float


def _profile(name: str, source: str, cfg: Mapping[str, Any]) -> SeasonalComfort:
    return SeasonalComfort(
        name=name,
        source=source,
        heating_enabled=bool(cfg.get("heating_enabled", True)),
        cooling_enabled=bool(cfg.get("cooling_enabled", True)),
        heat_below_c=float(cfg["heat_below_c"]),
        heat_target_c=float(cfg["heat_target_c"]),
        cool_above_c=float(cfg["cool_above_c"]),
        cool_target_c=float(cfg["cool_target_c"]),
        deadband_c=max(float(cfg.get("deadband_c", 0.0)), 0.0),
    )


def select_seasonal_comfort(
    *,
    now: datetime,
    policy: Mapping[str, Any],
    outdoor_recent_mean_c: float | None = None,
) -> SeasonalComfort:
    seasonal = policy.get("seasonal_comfort")
    if not isinstance(seasonal, Mapping) or not seasonal.get("enabled", False):
        return _profile("legacy", "legacy", policy["comfort"])

    profiles = seasonal["profiles"]
    selector = seasonal["selector"]
    if outdoor_recent_mean_c is not None:
        recent_mean = float(outdoor_recent_mean_c)
        if recent_mean <= float(selector["winter_max_recent_mean_c"]):
            name = "winter"
        elif recent_mean >= float(selector["summer_min_recent_mean_c"]):
            name = "summer"
        else:
            name = "shoulder"
        return _profile(name, "outdoor_recent_mean", profiles[name])

    month = int(now.month)
    fallback = selector["fallback_months"]
    for name in ("winter", "summer", "shoulder"):
        if month in {int(value) for value in fallback.get(name, ())}:
            return _profile(name, "calendar_fallback", profiles[name])
    return _profile("shoulder", "calendar_fallback", profiles["shoulder"])
