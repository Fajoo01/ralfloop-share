from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from .comfort import select_seasonal_comfort


@dataclass(frozen=True)
class CalendarEvent:
    event_id: str
    summary: str
    start: datetime
    end: datetime
    location: str = ""
    calendar_id: str = ""


def _fold(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def classify_event(event: CalendarEvent, policy: Mapping[str, Any]) -> tuple[bool, str]:
    cal = policy["calendar"]
    haystack = _fold(f"{event.summary} {event.location}")
    if any(_fold(marker) in haystack for marker in cal.get("remote_markers", ()) if marker):
        return False, "remote_event"
    location = _fold(event.location)
    if location:
        if any(_fold(marker) in location for marker in cal.get("location_markers", ()) if marker):
            return True, "explicit_sede_location"
        return False, "different_location"
    if event.calendar_id == str(cal.get("calendar_id") or "") and bool(cal.get("empty_location_means_sede")):
        return True, "calendar_default_sede"
    return False, "location_unknown"


def _lead_minutes(delta_c: float, rate_c_per_hour: float, timing: Mapping[str, Any]) -> int:
    rate = max(float(rate_c_per_hour), 0.1)
    raw = (max(delta_c, 0.0) / rate) * 60.0 + float(timing.get("buffer_minutes", 0))
    return int(max(float(timing["min_lead_minutes"]), min(float(timing["max_lead_minutes"]), raw)))


def plan_event(
    event: CalendarEvent,
    *,
    now: datetime,
    current_temperature_c: float | None,
    policy: Mapping[str, Any],
    boiler_available: bool = False,
    outdoor_recent_mean_c: float | None = None,
) -> dict[str, Any]:
    eligible, reason = classify_event(event, policy)
    base = {
        "site": "sede",
        "event_id": event.event_id,
        "summary": event.summary,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "eligible": eligible,
        "eligibility_reason": reason,
        "dry_run": True,
    }
    if not eligible:
        return {**base, "decision": "ignore"}
    if event.end <= now:
        return {**base, "decision": "ignore", "reason": "event_ended"}
    if current_temperature_c is None:
        return {**base, "decision": "observe_only", "reason": "indoor_temperature_unavailable"}

    comfort = select_seasonal_comfort(
        now=now, policy=policy, outdoor_recent_mean_c=outdoor_recent_mean_c
    )
    timing = policy["timing"]
    actuators = policy["actuators"]
    temp = float(current_temperature_c)
    heat_trigger = comfort.heat_below_c - comfort.deadband_c
    cool_trigger = comfort.cool_above_c + comfort.deadband_c
    season_meta = {
        "comfort_profile": comfort.name,
        "comfort_profile_source": comfort.source,
        "outdoor_recent_mean_c": outdoor_recent_mean_c,
    }
    if comfort.heating_enabled and temp < heat_trigger:
        target = comfort.heat_target_c
        lead = _lead_minutes(target - temp, float(timing["heat_rate_c_per_hour"]), timing)
        actuator = actuators["heating_preferred"] if boiler_available else actuators["heating_fallback_entity"]
        mode = "heat"
    elif comfort.cooling_enabled and temp > cool_trigger:
        target = comfort.cool_target_c
        lead = _lead_minutes(temp - target, float(timing["cool_rate_c_per_hour"]), timing)
        actuator = actuators["cooling_entity"]
        mode = "cool"
    else:
        return {**base, **season_meta, "decision": "no_climate_action", "current_temperature_c": temp, "reason": "within_seasonal_comfort_band"}

    action_at = event.start - timedelta(minutes=lead)
    stop_at = event.end + timedelta(minutes=float(timing.get("post_event_grace_minutes", 0)))
    return {
        **base,
        **season_meta,
        "decision": "precondition",
        "mode": mode,
        "actuator": actuator,
        "current_temperature_c": temp,
        "target_temperature_c": target,
        "lead_minutes": lead,
        "action_at": action_at.isoformat(),
        "stop_at": stop_at.isoformat(),
        "action_due": now >= action_at and now < event.end,
    }

def plan_event_with_weather(
    event: CalendarEvent,
    *,
    now: datetime,
    current_temperature_c: float | None,
    policy: Mapping[str, Any],
    boiler_available: bool = False,
) -> dict[str, Any]:
    # Import locally to keep the pure planner usable in tests/offline tools.
    from .meteo import fetch_outdoor_weather

    weather = fetch_outdoor_weather(policy)
    planned = plan_event(
        event,
        now=now,
        current_temperature_c=current_temperature_c,
        policy=policy,
        boiler_available=boiler_available,
        outdoor_recent_mean_c=weather.recent_mean_c,
    )
    return {
        **planned,
        "weather_status": weather.status,
        "weather_history_hours": weather.history_hours,
    }
