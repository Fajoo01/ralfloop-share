from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from .calendar_mcp import CalendarReadResult, GoogleCalendarMCPReader
from .meteo import OutdoorWeatherContext, fetch_outdoor_weather
from .planner import CalendarEvent, classify_event_kind, plan_event_with_weather
from .tuya_read import IndoorClimateContext, fetch_sede_indoor_temperature


@dataclass(frozen=True)
class CalendarBlock:
    block_id: str
    events: tuple[CalendarEvent, ...]
    start: datetime
    end: datetime


def _fingerprint(event: CalendarEvent) -> str:
    payload = {
        "summary": event.summary,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "location": event.location,
        "calendar_id": event.calendar_id,
        "description": event.description,
        "conference_url": event.conference_url,
        "all_day": event.all_day,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _event_row(event: CalendarEvent, policy: Mapping[str, Any]) -> dict[str, Any]:
    classification, reason = classify_event_kind(event, policy)
    return {
        "event_id": event.event_id,
        "summary": event.summary,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "location": event.location,
        "calendar_id": event.calendar_id,
        "conference_url": event.conference_url,
        "all_day": event.all_day,
        "classification": classification,
        "classification_reason": reason,
        "fingerprint": _fingerprint(event),
    }


def merge_sede_events(
    events: Sequence[CalendarEvent],
    policy: Mapping[str, Any],
) -> tuple[CalendarBlock, ...]:
    gap = timedelta(minutes=float((policy.get("calendar_runtime") or {}).get("merge_gap_minutes", 0)))
    eligible = [
        event for event in events
        if classify_event_kind(event, policy)[0] == "sede"
    ]
    eligible.sort(key=lambda item: (item.start, item.end, item.event_id))
    groups: list[list[CalendarEvent]] = []
    for event in eligible:
        if not groups or event.start > max(item.end for item in groups[-1]) + gap:
            groups.append([event])
        else:
            groups[-1].append(event)
    blocks: list[CalendarBlock] = []
    for group in groups:
        start = min(item.start for item in group)
        end = max(item.end for item in group)
        material = "\x00".join(
            sorted(item.event_id for item in group)
        ) + f"\x00{start.isoformat()}\x00{end.isoformat()}"
        blocks.append(CalendarBlock(
            block_id="sede-" + hashlib.sha256(material.encode()).hexdigest()[:16],
            events=tuple(group),
            start=start,
            end=end,
        ))
    return tuple(blocks)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(dict(value), handle, ensure_ascii=False, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _append_jsonl(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _state_paths(policy: Mapping[str, Any]) -> tuple[Path, Path]:
    runtime = policy.get("calendar_runtime") or {}
    configured = str(runtime.get("state_dir") or "~/.local/share/bottazzi/sede-climate")
    state_dir = Path(os.getenv("RALF_SEDE_CLIMATE_STATE_DIR", configured)).expanduser()
    return (
        state_dir / str(runtime.get("state_file") or "calendar-state.json"),
        state_dir / str(runtime.get("decision_log_file") or "decisions.jsonl"),
    )


def reconcile_events(
    current: Sequence[CalendarEvent],
    previous: Mapping[str, Any],
    *,
    now: datetime,
    window_end: datetime,
    listing_complete: bool,
    policy: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    old = previous.get("events") if isinstance(previous.get("events"), Mapping) else {}
    old = {str(key): value for key, value in old.items() if isinstance(value, Mapping)}
    rows = {event.event_id: _event_row(event, policy) for event in current}
    changes: list[dict[str, Any]] = []
    for event_id, row in rows.items():
        before = old.get(event_id)
        if before is None:
            status = "created"
        elif str(before.get("fingerprint") or "") != row["fingerprint"]:
            status = "modified"
        else:
            status = "unchanged"
        changes.append({
            "event_id": event_id,
            "change": status,
            "classification": row["classification"],
            "start": row["start"],
            "end": row["end"],
        })
    next_rows = dict(rows)
    if listing_complete:
        for event_id, before in old.items():
            if event_id in rows:
                continue
            try:
                old_start = datetime.fromisoformat(str(before.get("start")))
                old_end = datetime.fromisoformat(str(before.get("end")))
            except (TypeError, ValueError):
                continue
            if old_end <= now or old_start >= window_end:
                continue
            changes.append({
                "event_id": event_id,
                "change": "cancelled",
                "classification": before.get("classification"),
                "start": before.get("start"),
                "end": before.get("end"),
            })
    else:
        for event_id, before in old.items():
            if event_id in next_rows:
                continue
            try:
                old_end = datetime.fromisoformat(str(before.get("end")))
            except (TypeError, ValueError):
                continue
            if old_end > now:
                next_rows[event_id] = dict(before)
    changes.sort(key=lambda row: (str(row.get("start") or ""), row["event_id"]))
    return changes, next_rows


def _block_event(block: CalendarBlock, calendar_id: str) -> CalendarEvent:
    summaries = list(dict.fromkeys(item.summary for item in block.events))
    return CalendarEvent(
        event_id=block.block_id,
        summary=" + ".join(summaries),
        start=block.start,
        end=block.end,
        location=next((item.location for item in block.events if item.location), ""),
        calendar_id=calendar_id,
        description=" ".join(item.description for item in block.events if item.description),
        conference_url=next((item.conference_url for item in block.events if item.conference_url), ""),
    )


def _plan_block(
    block: CalendarBlock,
    *,
    now: datetime,
    indoor: IndoorClimateContext,
    weather: OutdoorWeatherContext,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    event = _block_event(block, str(policy["calendar"]["calendar_id"]))
    plan = plan_event_with_weather(
        event,
        now=now,
        current_temperature_c=indoor.reading.current_temperature_c,
        policy=policy,
        boiler_available=indoor.status == "ok",
        weather_context=weather,
    )
    return {
        **plan,
        "block_id": block.block_id,
        "member_event_ids": [item.event_id for item in block.events],
        "member_summaries": [item.summary for item in block.events],
        "block_start": block.start.isoformat(),
        "block_end": block.end.isoformat(),
        "action_at": plan.get("action_at"),
        "lead_minutes": plan.get("lead_minutes"),
        "stop_at": plan.get("stop_at"),
        "mode": plan.get("mode"),
        "actuator": plan.get("actuator"),
    }


def load_policy(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("site") != "sede":
        raise ValueError("sede_climate_policy_required")
    if value.get("dry_run") is not True:
        raise ValueError("calendar_climate_runtime_requires_dry_run")
    return value


def run_calendar_climate_cycle(
    policy: Mapping[str, Any],
    *,
    now: datetime | None = None,
    calendar_reader: Any | None = None,
    indoor_fetch: Callable[[Mapping[str, Any]], IndoorClimateContext] = fetch_sede_indoor_temperature,
    weather_fetch: Callable[[Mapping[str, Any]], OutdoorWeatherContext] = fetch_outdoor_weather,
    persist: bool = True,
) -> dict[str, Any]:
    if policy.get("site") != "sede" or policy.get("dry_run") is not True:
        raise ValueError("sede_dry_run_policy_required")
    tz = ZoneInfo(str(policy.get("timezone") or "Europe/Rome"))
    observed_at = now.astimezone(tz) if now is not None else datetime.now(tz)
    reader = calendar_reader or GoogleCalendarMCPReader(policy)
    calendar: CalendarReadResult = reader.list_upcoming(observed_at)
    indoor = indoor_fetch(policy)
    weather = weather_fetch(policy)
    blocks = merge_sede_events(calendar.events, policy)
    plans = [
        _plan_block(
            block,
            now=observed_at,
            indoor=indoor,
            weather=weather,
            policy=policy,
        )
        for block in blocks
        if block.end > observed_at
    ]
    state_path, log_path = _state_paths(policy)
    previous = _read_json(state_path) if persist else {}
    changes, next_events = reconcile_events(
        calendar.events,
        previous,
        now=observed_at,
        window_end=calendar.window_end,
        listing_complete=calendar.listing_complete,
        policy=policy,
    )
    event_rows = [_event_row(event, policy) for event in calendar.events]
    run_material = f"{observed_at.isoformat()}\x00{len(event_rows)}\x00{len(plans)}"
    run_id = hashlib.sha256(run_material.encode()).hexdigest()[:20]
    record = {
        "schema_version": 1,
        "run_id": run_id,
        "observed_at": observed_at.isoformat(),
        "site": "sede",
        "dry_run": True,
        "calendar": {
            "account": policy["calendar"]["account"],
            "calendar_id": policy["calendar"]["calendar_id"],
            "window_start": calendar.window_start.isoformat(),
            "window_end": calendar.window_end.isoformat(),
            "listed_count": calendar.listed_count,
            "listing_complete": calendar.listing_complete,
        },
        "indoor": {
            "status": indoor.status,
            "entity_id": indoor.entity_id,
            "site": indoor.site,
            "current_temperature_c": indoor.reading.current_temperature_c,
            "target_temperature_c": indoor.reading.target_temperature_c,
            "hvac_state": indoor.reading.hvac_state,
            "writes": indoor.writes,
            "sends": indoor.sends,
        },
        "weather": {
            "status": weather.status,
            "recent_temperature_mean_c": weather.recent_mean_c,
            "recent_temperature_hours": weather.history_hours,
        },
        "events": event_rows,
        "reconciliation": changes,
        "blocks": plans,
        "climate_writes": 0,
        "climate_sends": 0,
    }
    if persist:
        state = {
            "schema_version": 1,
            "site": "sede",
            "updated_at": observed_at.isoformat(),
            "window_end": calendar.window_end.isoformat(),
            "events": next_events,
            "last_run_id": run_id,
        }
        _append_jsonl(log_path, record)
        _write_json_atomic(state_path, state)
        record["persistence"] = {
            "state_path": str(state_path),
            "decision_log_path": str(log_path),
        }
    else:
        record["persistence"] = {"status": "disabled"}
    return record
