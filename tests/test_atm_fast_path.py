from __future__ import annotations

from pathlib import Path

import json
from datetime import datetime as RealDateTime

from openshell_backend import atm_telegram as atm


def direct_trip():
    return {
        "raw": {
            "Actions": [{
                "Leg": {
                    "TravelMode": 1,
                    "Journeys": [{"JourneyPattern": {"Code": "53"}}],
                }
            }]
        },
        "summary": {
            "duration": "21",
            "lines": ["53"],
            "first_line": "53",
            "board_stop_code": "12790",
            "live_wait": "4 min",
            "eta_seconds": 1500,
        },
    }


def test_official_direct_trip_skips_expensive_alternative_scan(monkeypatch):
    trip = direct_trip()
    monkeypatch.setattr(atm, "_resolve_destination", lambda _name: {
        "name": "shop", "label": "Shop", "lat": 45.512773, "lon": 9.246905
    })
    monkeypatch.setattr(atm, "_local_atm_realtime_route", lambda *_a, **_k: None)
    monkeypatch.setattr(atm, "_topology_direct_options", lambda *_a, **_k: [])
    monkeypatch.setattr(atm, "_atm_trip_plan", lambda *_a, **_k: trip)

    def must_not_run(*_args, **_kwargs):
        raise AssertionError("expensive alternative scan must not run")

    monkeypatch.setattr(atm, "_nearby_stops", must_not_run)
    monkeypatch.setattr(atm, "_nearby_route_lines", must_not_run)
    monkeypatch.setattr(atm, "_atm_one_transfer_options", must_not_run)
    monkeypatch.setattr(atm, "_atm_direct_fallback_options", must_not_run)

    result = atm._build_plan_impl(45.50671, 9.23554, "shop")
    assert result["route_mode"] == "official_atm_trip"
    assert result["official_route"] is trip
    assert result["source"] == "GiroMilano ATM direct fast path"


def test_multiple_topology_directs_return_before_trip_planner(monkeypatch):
    monkeypatch.setattr(atm, "_resolve_destination", lambda _name: {
        "name": "shop", "label": "Shop", "lat": 45.503224, "lon": 9.239615
    })
    monkeypatch.setattr(atm, "_local_atm_realtime_route", lambda *_a, **_k: None)
    options = [
        {"line": "A", "wait": "2 min"},
        {"line": "B", "wait": "4 min"},
        {"line": "C", "wait": "6 min"},
    ]
    monkeypatch.setattr(atm, "_topology_direct_options", lambda *_a, **_k: options)
    monkeypatch.setattr(
        atm,
        "_atm_trip_plan",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("trip planner must not run for multiple direct candidates")
        ),
    )

    result = atm._build_plan_impl(45.50671, 9.23554, "shop")
    assert result["route_mode"] == "direct_atm"
    assert result["direct_atm_options"] == options
    assert result["direct_atm_route"] is options[0]
    assert result["route_confidence"] == "medium"
    assert result["source"] == "GTFS topology; ATM realtime non disponibile"


def test_topology_candidates_are_ranked_from_data_not_line_names(monkeypatch, tmp_path):
    path = tmp_path / "topology.json"
    payload = {
        "version": 2,
        "patterns": [
            {
                "line": "X7",
                "direction": "0",
                "stops": [
                    {"id": "O1", "name": "Origin 1", "lat": 45.5000, "lon": 9.2000, "departure_s": 1000},
                    {"id": "D1", "name": "Dest 1", "lat": 45.5010, "lon": 9.2010, "arrival_s": 1120},
                ],
            },
            {
                "line": "K2",
                "direction": "1",
                "stops": [
                    {"id": "O2", "name": "Origin 2", "lat": 45.5002, "lon": 9.2000, "departure_s": 1000},
                    {"id": "D2", "name": "Dest 2", "lat": 45.5011, "lon": 9.2010, "arrival_s": 1130},
                ],
            },
            {
                "line": "Z9",
                "direction": "0",
                "stops": [
                    {"id": "O3", "name": "Far origin", "lat": 45.5070, "lon": 9.2000, "departure_s": 1000},
                    {"id": "D3", "name": "Dest 3", "lat": 45.5010, "lon": 9.2010, "arrival_s": 1140},
                ],
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(atm, "ATM_DIRECT_TOPOLOGY_PATH", path)
    monkeypatch.setattr(atm, "_ATM_DIRECT_TOPOLOGY_CACHE", None)
    monkeypatch.setattr(atm, "ATM_DIRECT_ACCESS_SLACK_M", 250)

    rows = atm._topology_direct_candidates(
        45.5000, 9.2000, 45.5010, 9.2010, limit=3
    )
    assert [row["line"] for row in rows] == ["X7", "K2"]
    assert rows[0]["travel_seconds"] == 120
    assert rows[1]["travel_seconds"] == 130


def test_render_multiple_direct_candidates():
    plan = {
        "route_mode": "direct_atm",
        "destination": {"name": "shop", "label": "Shop"},
        "direct_atm_route": {"line": "A"},
        "direct_atm_options": [
            {
                "line": "A", "wait": "2 min", "origin_stop_name": "Stop A",
                "dest_stop_name": "Stop D", "stops_count": 2,
                "destination_arrival_at": RealDateTime(2026, 9, 15, 17, 5),
            },
            {
                "line": "B", "wait": "4 min", "origin_stop_name": "Stop B",
                "dest_stop_name": "Stop D", "stops_count": 3,
            },
        ],
        "official_route_url": "https://example.test/atm",
    }
    text = atm.render_reply(plan)
    assert "Dirette utili: A, B" in text
    assert "Attese reali: A: 2 min, B: 4 min" in text
    assert "A: Stop A → Stop D (2 fermate), arrivo stimato 17:05" in text


def test_topology_options_use_provider_batch_once(monkeypatch):
    candidates = [
        {
            "line": "X7", "direction": "0", "origin_stop_code": "O1",
            "origin_stop_name": "Origin 1", "origin_stop_lat": 45.5,
            "origin_stop_lon": 9.2, "origin_distance_m": 20,
            "dest_stop_code": "D1", "dest_stop_name": "Dest 1",
            "dest_stop_lat": 45.501, "dest_stop_lon": 9.201,
            "dest_distance_m": 30, "access_m": 50, "stops_count": 2,
            "travel_seconds": 180,
        },
        {
            "line": "K2", "direction": "1", "origin_stop_code": "O2",
            "origin_stop_name": "Origin 2", "origin_stop_lat": 45.5,
            "origin_stop_lon": 9.2, "origin_distance_m": 25,
            "dest_stop_code": "D2", "dest_stop_name": "Dest 2",
            "dest_stop_lat": 45.501, "dest_stop_lon": 9.201,
            "dest_distance_m": 35, "access_m": 60, "stops_count": 2,
            "travel_seconds": 200,
        },
    ]
    monkeypatch.setattr(atm, "_topology_direct_candidates", lambda *_a, **_k: [dict(x) for x in candidates])

    class Provider:
        def __init__(self):
            self.calls = []

        def batch(self, queries):
            self.calls.append(queries)
            return {
                "O1": {"observations": [{"line": "X7", "direction": "0", "wait": "2 min"}], "arrivals": {}},
                "O2": {"observations": [{"line": "K2", "direction": "1", "wait": "3 min"}], "arrivals": {}},
            }

        def snapshot(self, *_a, **_k):
            raise AssertionError("snapshot singolo non deve essere usato quando batch è disponibile")

    provider = Provider()
    token = atm._ATM_REALTIME_PROVIDER.set(provider)
    try:
        rows = atm._topology_direct_options(45.5, 9.2, 45.501, 9.201, limit=3)
    finally:
        atm._ATM_REALTIME_PROVIDER.reset(token)

    assert len(provider.calls) == 1
    assert {q["stop_code"] for q in provider.calls[0]} == {"O1", "O2"}
    assert [row["line"] for row in rows] == ["X7", "K2"]
    assert all(row["wait_source"] == "atm_live" for row in rows)


def test_legacy_snapshot_uses_short_bounded_timeout(monkeypatch):
    seen = {}

    def fake_fetch(path, timeout=None):
        seen["path"] = path
        seen["timeout"] = timeout
        return {"Lines": []}

    monkeypatch.setattr(atm, "_tpportal_fetch_json_direct", fake_fetch)
    result = atm._legacy_stop_snapshot("O1", ["X7"])
    assert result["status"] == "not_available"
    assert seen["path"].endswith("tpl/stops/O1/linesummary")
    assert seen["timeout"] == 2.2


def test_single_static_direct_skips_remote_trip_planner(monkeypatch):
    monkeypatch.setattr(atm, "_resolve_destination", lambda _name: {
        "name": "shop", "label": "Shop", "lat": 45.501, "lon": 9.201
    })
    monkeypatch.setattr(atm, "_local_atm_realtime_route", lambda *_a, **_k: None)
    option = {
        "line": "X7", "wait": "n/d", "wait_source": "not_available",
        "eta_seconds": None,
    }
    monkeypatch.setattr(atm, "_topology_direct_options", lambda *_a, **_k: [option])
    monkeypatch.setattr(
        atm, "_atm_trip_plan",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("remote trip planner must not run for a known short direct route")
        ),
    )
    result = atm._build_plan_impl(45.500, 9.200, "shop")
    assert result["route_mode"] == "direct_atm"
    assert result["route_confidence"] == "medium"
    assert result["direct_atm_options"] == [option]


def test_empty_provider_batch_does_not_retry_single_snapshots(monkeypatch):
    candidate = {
        "line": "X7", "direction": "0", "origin_stop_code": "O1",
        "origin_stop_name": "Origin", "origin_stop_lat": 45.5,
        "origin_stop_lon": 9.2, "origin_distance_m": 20,
        "dest_stop_code": "D1", "dest_stop_name": "Dest",
        "dest_stop_lat": 45.501, "dest_stop_lon": 9.201,
        "dest_distance_m": 30, "access_m": 50, "stops_count": 2,
        "travel_seconds": 180,
    }
    monkeypatch.setattr(atm, "_topology_direct_candidates", lambda *_a, **_k: [dict(candidate)])

    class Provider:
        def batch(self, _queries):
            return {}
        def snapshot(self, *_a, **_k):
            raise AssertionError("batch provider must not be retried with snapshot")

    token = atm._ATM_REALTIME_PROVIDER.set(Provider())
    try:
        rows = atm._topology_direct_options(45.5, 9.2, 45.501, 9.201)
    finally:
        atm._ATM_REALTIME_PROVIDER.reset(token)
    assert len(rows) == 1
    assert rows[0]["wait_source"] == "not_available"


def test_atm_broker_dropin_enables_bounded_concurrency():
    dropin = Path(
        "deploy/systemd/ralf-atm-mcp-broker.service.d/95-atm-concurrency.conf"
    ).read_text(encoding="utf-8")
    assert "RALF_MCP_IDLE_TIMEOUT=5" in dropin
    assert "RALF_MCP_MAX_CLIENTS=4" in dropin
