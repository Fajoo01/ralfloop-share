from __future__ import annotations

from datetime import datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from integrations.bottazzi_climate.calendar_mcp import (
    CalendarReadResult,
    parse_event_detail,
)
from integrations.bottazzi_climate.energy_economics import unavailable_energy_context
from integrations.bottazzi_climate.meteo import OutdoorWeatherContext
from integrations.bottazzi_climate.planner import CalendarEvent, classify_event_kind
from integrations.bottazzi_climate.runtime import (
    load_policy,
    merge_sede_events,
    reconcile_events,
    run_calendar_climate_cycle,
)
from integrations.bottazzi_climate.thermostat import ThermostatReading
from integrations.bottazzi_climate.tuya_read import IndoorClimateContext


TZ = ZoneInfo("Europe/Rome")


def policy() -> dict:
    return json.loads(Path("config/sede_climate_policy.json").read_text())


def event(event_id: str, start: datetime, end: datetime, **kwargs) -> CalendarEvent:
    return CalendarEvent(
        event_id, kwargs.pop("summary", event_id), start, end,
        calendar_id=kwargs.pop("calendar_id", "fabio@tiremminnanz.com"), **kwargs,
    )


def test_real_calendar_cobas_detail_is_parsed_as_sede_default():
    text = """## Sindacato Cobas

**When:** Fri, Sep 18 18:00–20:30
**Organizer:** fabio@tiremminnanz.com
"""
    parsed = parse_event_detail(
        text,
        event_id="2r1lan0dh8b8ntln2mieoc2m09_20260918T160000Z",
        calendar_id="fabio@tiremminnanz.com",
        timezone="Europe/Rome",
        reference=datetime(2026, 9, 17, 15, 34, tzinfo=TZ),
    )
    assert parsed.start == datetime(2026, 9, 18, 18, 0, tzinfo=TZ)
    assert parsed.end == datetime(2026, 9, 18, 20, 30, tzinfo=TZ)
    assert classify_event_kind(parsed, policy()) == ("sede", "calendar_default_sede")


def test_explicit_sede_location_wins_over_hybrid_meet_link():
    text = """## Sportello Legale Sociale

**When:** Wed, Sep 23 16:00–19:00
**Where:** Tiremm Innanz, Via Privata Federico Jarach, 8, 20128 Milano MI, Italia
**Meet:** https://meet.google.com/tpd-mmck-feg
"""
    parsed = parse_event_detail(
        text, event_id="sportello", calendar_id="fabio@tiremminnanz.com",
        timezone="Europe/Rome", reference=datetime(2026, 9, 17, tzinfo=TZ),
    )
    assert classify_event_kind(parsed, policy()) == ("sede", "explicit_sede_location")


def test_conference_only_or_online_title_is_remote():
    now = datetime(2026, 9, 17, 15, 0, tzinfo=TZ)
    meet = event(
        "meet", now + timedelta(hours=1), now + timedelta(hours=2),
        summary="Riunione", conference_url="https://meet.google.com/abc-defg-hij",
    )
    title = event(
        "online", now + timedelta(hours=2), now + timedelta(hours=3),
        summary="Tedesco (online)",
    )
    assert classify_event_kind(meet, policy())[0] == "online"
    assert classify_event_kind(title, policy())[0] == "online"


def test_other_physical_location_is_not_sede():
    now = datetime(2026, 9, 17, 15, 0, tzinfo=TZ)
    elsewhere = event(
        "elsewhere", now + timedelta(hours=1), now + timedelta(hours=2),
        location="Piazza Duomo 1, Milano",
    )
    assert classify_event_kind(elsewhere, policy()) == ("other", "different_location")


def test_consecutive_and_overlapping_sede_events_merge_into_one_block():
    p = policy(); now = datetime(2026, 9, 18, 12, 0, tzinfo=TZ)
    rows = [
        event("a", now + timedelta(hours=1), now + timedelta(hours=2)),
        event("b", now + timedelta(hours=2), now + timedelta(hours=3)),
        event("c", now + timedelta(hours=2, minutes=30), now + timedelta(hours=4)),
    ]
    blocks = merge_sede_events(rows, p)
    assert len(blocks) == 1
    assert [item.event_id for item in blocks[0].events] == ["a", "b", "c"]
    assert blocks[0].start == rows[0].start
    assert blocks[0].end == rows[2].end


def test_moved_and_cancelled_events_are_reconciled():
    p = policy(); now = datetime(2026, 9, 18, 12, 0, tzinfo=TZ)
    original_a = event("a", now + timedelta(hours=4), now + timedelta(hours=5))
    original_b = event("b", now + timedelta(hours=6), now + timedelta(hours=7))
    old = {
        "events": {
            row.event_id: {
                "event_id": row.event_id,
                "start": row.start.isoformat(),
                "end": row.end.isoformat(),
                "classification": "sede",
                "fingerprint": __import__("integrations.bottazzi_climate.runtime", fromlist=["_fingerprint"])._fingerprint(row),
            }
            for row in (original_a, original_b)
        }
    }
    moved_a = event("a", original_a.start + timedelta(hours=1), original_a.end + timedelta(hours=1))
    changes, state = reconcile_events(
        [moved_a], old, now=now, window_end=now + timedelta(days=14),
        listing_complete=True, policy=p,
    )
    change_map = {row["event_id"]: row["change"] for row in changes}
    assert change_map == {"a": "modified", "b": "cancelled"}
    assert set(state) == {"a"}


def test_incomplete_listing_never_infers_cancellation():
    p = policy(); now = datetime(2026, 9, 18, 12, 0, tzinfo=TZ)
    future = event("old", now + timedelta(hours=6), now + timedelta(hours=7))
    old = {"events": {"old": {
        "event_id": "old", "start": future.start.isoformat(), "end": future.end.isoformat(),
        "classification": "sede", "fingerprint": "old-fingerprint",
    }}}
    changes, state = reconcile_events(
        [], old, now=now, window_end=now + timedelta(days=14),
        listing_complete=False, policy=p,
    )
    assert changes == []
    assert "old" in state


class FakeCalendarReader:
    def __init__(self, events, now):
        self.events = tuple(events)
        self.now = now

    def list_upcoming(self, _now):
        return CalendarReadResult(
            events=self.events,
            window_start=self.now,
            window_end=self.now + timedelta(days=14),
            listing_complete=True,
            listed_count=len(self.events),
        )


def indoor(temp: float) -> IndoorClimateContext:
    return IndoorClimateContext(
        reading=ThermostatReading(temp, 19.0, "heat_cool"),
        status="ok",
        entity_id="climate.termostato",
        site="sede",
        writes=0,
        sends=0,
        raw_state={"current_temperature": temp / 5.0, "temperature": 3.8},
    )


def test_runtime_persists_plan_fields_without_climate_writes(tmp_path, monkeypatch):
    p = policy(); p["calendar_runtime"]["state_dir"] = str(tmp_path)
    now = datetime(2026, 1, 17, 12, 0, tzinfo=TZ)
    e = event("real-shape", now + timedelta(hours=2), now + timedelta(hours=3), summary="Riunione Tiremm")
    result = run_calendar_climate_cycle(
        p, now=now, calendar_reader=FakeCalendarReader([e], now),
        indoor_fetch=lambda _: indoor(17.0),
        weather_fetch=lambda _: OutdoorWeatherContext(10.0, 168, "ok"),
        energy_fetch=lambda *args, **kwargs: unavailable_energy_context("test_disabled"),
    )
    assert result["dry_run"] is True
    assert result["climate_writes"] == 0 and result["climate_sends"] == 0
    assert result["indoor"]["writes"] == 0 and result["indoor"]["sends"] == 0
    plan = result["blocks"][0]
    assert plan["decision"] == "precondition"
    assert plan["mode"] == "heat"
    assert plan["actuator"] == "climate.termostato"
    assert plan["lead_minutes"] is not None
    assert plan["action_at"] is not None
    assert plan["stop_at"] is not None
    assert Path(result["persistence"]["state_path"]).is_file()
    log = Path(result["persistence"]["decision_log_path"])
    assert log.is_file() and '"climate_writes": 0' in log.read_text()


def test_runtime_uses_calendar_fallback_when_weather_history_unavailable(tmp_path):
    p = policy(); p["calendar_runtime"]["state_dir"] = str(tmp_path)
    now = datetime(2026, 9, 18, 12, 0, tzinfo=TZ)
    e = event("fallback", now + timedelta(hours=2), now + timedelta(hours=3))
    result = run_calendar_climate_cycle(
        p, now=now, calendar_reader=FakeCalendarReader([e], now),
        indoor_fetch=lambda _: indoor(18.0),
        weather_fetch=lambda _: OutdoorWeatherContext(None, 12, "insufficient_history"),
        energy_fetch=lambda *args, **kwargs: unavailable_energy_context("test_disabled"),
    )
    plan = result["blocks"][0]
    assert plan["comfort_profile"] == "shoulder"
    assert plan["comfort_profile_source"] == "calendar_fallback"
    assert plan["weather_status"] == "insufficient_history"


def test_online_and_other_location_do_not_create_climate_blocks(tmp_path):
    p = policy(); p["calendar_runtime"]["state_dir"] = str(tmp_path)
    now = datetime(2026, 9, 18, 12, 0, tzinfo=TZ)
    rows = [
        event("online", now + timedelta(hours=1), now + timedelta(hours=2), summary="Tedesco (online)"),
        event("other", now + timedelta(hours=3), now + timedelta(hours=4), location="Piazza Duomo 1, Milano"),
    ]
    result = run_calendar_climate_cycle(
        p, now=now, calendar_reader=FakeCalendarReader(rows, now),
        indoor_fetch=lambda _: indoor(23.0),
        weather_fetch=lambda _: OutdoorWeatherContext(22.0, 168, "ok"),
        energy_fetch=lambda *args, **kwargs: unavailable_energy_context("test_disabled"),
    )
    assert result["blocks"] == []
    classes = {row["event_id"]: row["classification"] for row in result["events"]}
    assert classes == {"online": "online", "other": "other"}


def test_all_day_event_is_ignored_for_climate():
    p = policy(); now = datetime(2026, 9, 18, 12, 0, tzinfo=TZ)
    row = event(
        "all-day", now + timedelta(days=1), now + timedelta(days=2),
        all_day=True,
    )
    assert classify_event_kind(row, p) == ("other", "all_day_event")


def test_load_policy_refuses_non_dry_run(tmp_path):
    p = policy(); p["dry_run"] = False
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(p))
    with pytest.raises(ValueError, match="requires_dry_run"):
        load_policy(path)
