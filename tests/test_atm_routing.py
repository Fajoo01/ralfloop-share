from __future__ import annotations

from datetime import datetime as RealDateTime

from openshell_backend import atm_telegram as atm


class FixedDateTime(RealDateTime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 10, 7, 40, 0)
        if tz is not None:
            return value.replace(tzinfo=tz)
        return value


def walking_trip():
    return {
        "TripId": "walking",
        "TotalDuration": 9,
        "Actions": [
            {
                "ActionDescription": "Vai a piedi",
                "Leg": {
                    "TravelMode": 0,
                    "Length": 300,
                    "Journeys": [],
                },
            }
        ],
    }


def transit_trip():
    return {
        "TripId": "bus44",
        "TotalDuration": 27,
        "Actions": [
            {
                "ActionType": 4,
                "ActionDescription": "Prendi la linea 44",
                "Place": {
                    "Code": "12788",
                    "Description": "Via P.te Nuovo Via Biumi",
                },
                "Leg": {
                    "TravelMode": 3,
                    "Length": 0,
                    "Journeys": [
                        {
                            "JourneyPattern": {
                                "Code": "44",
                                "Line": {
                                    "LineCode": "44",
                                },
                            }
                        }
                    ],
                },
            }
        ],
    }


def test_trip_plan_skips_walking_only_first_solution(monkeypatch):
    monkeypatch.setattr(atm, "datetime", FixedDateTime)

    monkeypatch.setattr(
        atm,
        "_browser_fetch_json",
        lambda _path: {
            "Trips": [
                walking_trip(),
                transit_trip(),
            ]
        },
    )

    monkeypatch.setattr(
        atm,
        "_atm_live_minutes",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "arrivals": {
                "44": "5 min",
            },
        },
    )

    result = atm._atm_trip_plan(
        45.507373,
        9.236441,
        45.503443,
        9.220793,
        "destinazione",
    )

    assert result is not None
    assert result["raw"]["TripId"] == "bus44"
    assert result["summary"]["first_line"] == "44"


def test_destination_eta_includes_initial_live_wait(monkeypatch):
    monkeypatch.setattr(atm, "datetime", FixedDateTime)

    monkeypatch.setattr(
        atm,
        "_browser_fetch_json",
        lambda _path: {
            "Trips": [
                transit_trip(),
            ]
        },
    )

    monkeypatch.setattr(
        atm,
        "_atm_live_minutes",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "arrivals": {
                "44": "5 min",
            },
        },
    )

    result = atm._atm_trip_plan(
        45.507373,
        9.236441,
        45.503443,
        9.220793,
        "destinazione",
    )

    assert result is not None

    summary = result["summary"]

    assert summary["live_wait"] == "5 min"
    assert summary["vehicle_eta"] == "07:45"

    # 07:40 + 5 minuti di attesa + 27 minuti strutturali.
    assert summary["destination_eta"] == "08:12"


def test_in_arrivo_means_zero_minutes():
    assert atm._minutes_value("in arrivo") == 0
    assert atm._minutes_value("In Arrivo") == 0

# GTFS_SCHEDULE_TESTS_START

def _write_gtfs(tmp_path, *, exception_rows=""):
    import zipfile

    path = tmp_path / "gtfs.zip"

    files = {
        "routes.txt": """route_id,agency_id,route_short_name,route_long_name,route_type
M1,TEST,1,m1 - linea rossa,1
""",
        "stops.txt": """stop_id,stop_name,stop_lat,stop_lon
ALPHA,Alpha,45.500000,9.200000
BETA,Beta,45.490000,9.190000
""",
        "calendar.txt": """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
WEEK,1,1,1,1,1,0,0,20260907,20260930
EXTRA,0,0,0,0,0,0,0,20260907,20260930
""",
        "calendar_dates.txt": (
            "service_id,date,exception_type\n" + exception_rows
        ),
        "trips.txt": """route_id,service_id,trip_id,trip_headsign,direction_id
M1,WEEK,T1,Rho,0
M1,WEEK,T2,Bisceglie,0
M1,EXTRA,TX,Rho,0
""",
        "stop_times.txt": """trip_id,arrival_time,departure_time,stop_id,stop_sequence
T1,07:41:38,07:41:38,ALPHA,1
T1,07:44:22,07:44:22,BETA,2
T2,07:44:38,07:44:38,ALPHA,1
T2,07:47:22,07:47:22,BETA,2
TX,07:43:00,07:43:00,ALPHA,1
TX,07:46:00,07:46:00,BETA,2
""",
    }

    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body)

    return path


def test_gtfs_selects_first_catchable_departure(tmp_path):
    gtfs = _write_gtfs(tmp_path)

    not_before = RealDateTime(2026, 9, 10, 7, 42, 0)

    result = atm._gtfs_next_departure(
        "M1",
        45.500000,
        9.200000,
        45.490000,
        9.190000,
        not_before,
        gtfs_path=gtfs,
    )

    assert result is not None
    assert result["line"] == "M1"

    # 07:41:38 è già persa: va presa quella delle 07:44:38.
    assert result["departure_at"] == RealDateTime(
        2026, 9, 10, 7, 44, 38
    )
    assert result["arrival_at"] == RealDateTime(
        2026, 9, 10, 7, 47, 22
    )
    assert result["wait_seconds"] == 158
    assert result["source"] == "gtfs_scheduled"


def test_gtfs_calendar_dates_overrides_regular_service(tmp_path):
    gtfs = _write_gtfs(
        tmp_path,
        exception_rows=(
            "WEEK,20260910,2\n"
            "EXTRA,20260910,1\n"
        ),
    )

    not_before = RealDateTime(2026, 9, 10, 7, 42, 0)

    result = atm._gtfs_next_departure(
        "M1",
        45.500000,
        9.200000,
        45.490000,
        9.190000,
        not_before,
        gtfs_path=gtfs,
    )

    assert result is not None

    # WEEK è rimosso per oggi; EXTRA è aggiunto.
    assert result["trip_id"] == "TX"
    assert result["departure_at"] == RealDateTime(
        2026, 9, 10, 7, 43, 0
    )
    assert result["arrival_at"] == RealDateTime(
        2026, 9, 10, 7, 46, 0
    )
    assert result["source"] == "gtfs_scheduled"



# ONE_TRANSFER_TESTS_START

def _pattern_stop(code, name, lat, lon):
    return {
        "Code": code,
        "Description": name,
        "Location": {
            "Y": lat,
            "X": lon,
        },
    }


def test_one_transfer_discovers_surface_to_metro_and_ranks_real_eta(monkeypatch):
    now = RealDateTime(2026, 9, 10, 7, 40, 0)

    origin = (45.500000, 9.200000)
    transfer = (45.505000, 9.205000)
    destination = (45.510000, 9.210000)

    board = _pattern_stop(
        "10001",
        "Fermata origine",
        *origin,
    )

    interchange = _pattern_stop(
        "10002",
        "Interscambio",
        *transfer,
    )

    def fake_pattern(jp):
        if jp == "73|0":
            return [
                board,
                _pattern_stop(
                    "10003",
                    "Fermata intermedia",
                    45.502000,
                    9.202000,
                ),
                interchange,
            ]

        return []

    monkeypatch.setattr(
        atm,
        "_atm_journey_pattern_stops",
        fake_pattern,
    )

    def fake_nearby_lines(lat, lon, radius_m=85, limit=12):
        if abs(lat - transfer[0]) < 0.00001:
            return [
                {
                    "line": "73",
                    "route": "bus",
                    "name": "73",
                    "operator": "ATM",
                },
                {
                    "line": "M2",
                    "route": "subway",
                    "name": "M2",
                    "operator": "ATM",
                },
            ]

        return [
            {
                "line": "73",
                "route": "bus",
                "name": "73",
                "operator": "ATM",
            }
        ]

    monkeypatch.setattr(
        atm,
        "_nearby_route_lines",
        fake_nearby_lines,
    )

    monkeypatch.setattr(
        atm,
        "_gtfs_subway_lines_near",
        lambda lat, lon, **kwargs: (
            ["M2"]
            if abs(lat - transfer[0]) < 0.00001
            else []
        ),
    )

    monkeypatch.setattr(
        atm,
        "_atm_live_minutes",
        lambda stop, lines: {
            "status": "ok",
            "arrivals": {
                "73": "2 min",
            },
            "source": "fixture_live",
        },
    )

    calls = []

    def fake_gtfs(
        line,
        origin_lat,
        origin_lon,
        dest_lat,
        dest_lon,
        not_before,
        *,
        gtfs_path=None,
    ):
        calls.append((line, not_before))

        if line == "73":
            # Per la prima tratta ci interessa il tempo di marcia:
            # il mezzo arriva live fra 2 minuti e poi impiega 8 minuti.
            return {
                "line": "73",
                "departure_at": RealDateTime(
                    2026, 9, 10, 7, 45, 0
                ),
                "arrival_at": RealDateTime(
                    2026, 9, 10, 7, 53, 0
                ),
                "travel_seconds": 8 * 60,
                "wait_seconds": 5 * 60,
                "source": "gtfs_scheduled",
            }

        if line == "M2":
            # Il candidato dalla fermata più vicina arriva al cambio
            # alle 07:50. Il builder può valutare anche una fermata
            # di salita successiva dello stesso journey pattern.
            assert not_before >= RealDateTime(
                2026, 9, 10, 7, 50, 0
            )
            assert not_before <= RealDateTime(
                2026, 9, 10, 7, 53, 0
            )

            return {
                "line": "M2",
                "departure_at": RealDateTime(
                    2026, 9, 10, 7, 53, 0
                ),
                "arrival_at": RealDateTime(
                    2026, 9, 10, 7, 58, 0
                ),
                "travel_seconds": 5 * 60,
                "wait_seconds": 3 * 60,
                "source": "gtfs_scheduled",
            }

        return None

    monkeypatch.setattr(
        atm,
        "_gtfs_next_departure",
        fake_gtfs,
    )

    options = atm._atm_one_transfer_options(
        origin[0],
        origin[1],
        destination[0],
        destination[1],
        ["73"],
        now=now,
    )

    assert options

    best = options[0]

    assert best["first_line"] == "73"
    assert best["second_line"] == "M2"
    assert best["transfer_stop_code"] == "10002"
    assert best["transfer_stop_name"] == "Interscambio"

    assert best["first_wait"] == "2 min"
    assert best["first_wait_source"] == "atm_live"
    assert best["second_wait_source"] == "gtfs_scheduled"

    assert best["transfer_arrival_at"] == RealDateTime(
        2026, 9, 10, 7, 50, 0
    )

    assert best["second_departure_at"] == RealDateTime(
        2026, 9, 10, 7, 53, 0
    )

    assert best["destination_arrival_at"] == RealDateTime(
        2026, 9, 10, 7, 58, 0
    )

    assert best["eta_seconds"] == 18 * 60

    # La prima tratta deve usare GTFS solo per la durata;
    # il suo orario programmato non deve sostituire il live ATM.
    assert calls[0][0] == "73"
    assert calls[1][0] == "M2"


def test_one_transfer_does_not_catch_vehicle_before_reaching_stop(monkeypatch):
    now = RealDateTime(2026, 9, 10, 7, 40, 0)

    # Circa 400 m dall'origine: servono ~5 minuti a piedi.
    origin = (45.500000, 9.200000)
    board_pos = (45.503600, 9.200000)
    transfer = (45.508000, 9.205000)
    destination = (45.515000, 9.210000)

    board = _pattern_stop(
        "20001",
        "Fermata lontana",
        *board_pos,
    )

    interchange = _pattern_stop(
        "20002",
        "Interscambio",
        *transfer,
    )

    monkeypatch.setattr(
        atm,
        "_atm_journey_pattern_stops",
        lambda jp: (
            [board, interchange]
            if jp == "73|0"
            else []
        ),
    )

    monkeypatch.setattr(
        atm,
        "_nearby_route_lines",
        lambda lat, lon, radius_m=85, limit=12: (
            [
                {
                    "line": "73",
                    "route": "bus",
                    "name": "73",
                    "operator": "ATM",
                },
                {
                    "line": "M2",
                    "route": "subway",
                    "name": "M2",
                    "operator": "ATM",
                },
            ]
            if abs(lat - transfer[0]) < 0.00001
            else []
        ),
    )

    # Il 73 live arriva alle 07:42: impossibile raggiungerlo.
    monkeypatch.setattr(
        atm,
        "_gtfs_subway_lines_near",
        lambda lat, lon, **kwargs: (
            ["M2"]
            if abs(lat - transfer[0]) < 0.00001
            else []
        ),
    )

    monkeypatch.setattr(
        atm,
        "_atm_live_minutes",
        lambda stop, lines: {
            "status": "ok",
            "arrivals": {"73": "2 min"},
            "source": "fixture_live",
        },
    )

    calls = []

    def fake_gtfs(
        line,
        origin_lat,
        origin_lon,
        dest_lat,
        dest_lon,
        not_before,
        *,
        gtfs_path=None,
    ):
        calls.append((line, not_before))

        if line == "73":
            # Arrivati alla fermata verso 07:45,
            # la prima corsa programmata prendibile è 07:47.
            assert not_before >= RealDateTime(
                2026, 9, 10, 7, 45, 0
            )

            return {
                "line": "73",
                "departure_at": RealDateTime(
                    2026, 9, 10, 7, 47, 0
                ),
                "arrival_at": RealDateTime(
                    2026, 9, 10, 7, 55, 0
                ),
                "travel_seconds": 8 * 60,
                "wait_seconds": 2 * 60,
                "source": "gtfs_scheduled",
            }

        if line == "M2":
            assert not_before == RealDateTime(
                2026, 9, 10, 7, 55, 0
            )

            return {
                "line": "M2",
                "departure_at": RealDateTime(
                    2026, 9, 10, 7, 58, 0
                ),
                "arrival_at": RealDateTime(
                    2026, 9, 10, 8, 3, 0
                ),
                "travel_seconds": 5 * 60,
                "wait_seconds": 3 * 60,
                "source": "gtfs_scheduled",
            }

        return None

    monkeypatch.setattr(
        atm,
        "_gtfs_next_departure",
        fake_gtfs,
    )

    options = atm._atm_one_transfer_options(
        origin[0],
        origin[1],
        destination[0],
        destination[1],
        ["73"],
        now=now,
    )

    assert options

    best = options[0]

    # Non deve fingere di aver preso il live delle 07:42.
    assert best["first_wait_source"] == "gtfs_scheduled"
    assert best["first_departure_at"] == RealDateTime(
        2026, 9, 10, 7, 47, 0
    )
    assert best["transfer_arrival_at"] == RealDateTime(
        2026, 9, 10, 7, 55, 0
    )
    assert best["destination_arrival_at"] == RealDateTime(
        2026, 9, 10, 8, 3, 0
    )
    assert best["eta_seconds"] == 23 * 60

    assert calls[0][0] == "73"
    assert calls[1][0] == "M2"


def test_gtfs_reports_access_distances(tmp_path):
    gtfs = _write_gtfs(tmp_path)

    # ~100 m a nord di ALPHA e ~100 m a sud di BETA.
    result = atm._gtfs_next_departure(
        "M1",
        45.500900,
        9.200000,
        45.489100,
        9.190000,
        RealDateTime(2026, 9, 10, 7, 40, 0),
        gtfs_path=gtfs,
    )

    assert result is not None

    assert 90 <= result["origin_distance_m"] <= 110
    assert 90 <= result["dest_distance_m"] <= 110

    assert result["origin_stop_id"] == "ALPHA"
    assert result["dest_stop_id"] == "BETA"


def test_one_transfer_includes_transfer_and_final_walk(monkeypatch):
    now = RealDateTime(2026, 9, 10, 7, 40, 0)

    origin = (45.500000, 9.200000)
    transfer = (45.505000, 9.205000)
    destination = (45.515000, 9.215000)

    board = _pattern_stop(
        "30001",
        "Fermata origine",
        *origin,
    )

    interchange = _pattern_stop(
        "30002",
        "Interscambio",
        *transfer,
    )

    monkeypatch.setattr(
        atm,
        "_atm_journey_pattern_stops",
        lambda jp: (
            [board, interchange]
            if jp == "73|0"
            else []
        ),
    )

    monkeypatch.setattr(
        atm,
        "_nearby_route_lines",
        lambda lat, lon, radius_m=85, limit=12: (
            [
                {
                    "line": "73",
                    "route": "bus",
                    "name": "73",
                    "operator": "ATM",
                },
                {
                    "line": "M2",
                    "route": "subway",
                    "name": "M2",
                    "operator": "ATM",
                },
            ]
            if abs(lat - transfer[0]) < 0.00001
            else []
        ),
    )

    monkeypatch.setattr(
        atm,
        "_gtfs_subway_lines_near",
        lambda lat, lon, **kwargs: (
            ["M2"]
            if abs(lat - transfer[0]) < 0.00001
            else []
        ),
    )

    monkeypatch.setattr(
        atm,
        "_atm_live_minutes",
        lambda stop, lines: {
            "status": "ok",
            "arrivals": {"73": "2 min"},
            "source": "fixture_live",
        },
    )

    m2_calls = []

    def fake_gtfs(
        line,
        origin_lat,
        origin_lon,
        dest_lat,
        dest_lon,
        not_before,
        *,
        gtfs_path=None,
    ):
        if line == "73":
            return {
                "line": "73",
                "departure_at": RealDateTime(
                    2026, 9, 10, 7, 42, 0
                ),
                "arrival_at": RealDateTime(
                    2026, 9, 10, 7, 50, 0
                ),
                "travel_seconds": 8 * 60,
                "wait_seconds": 2 * 60,
                "origin_distance_m": 0,
                "dest_distance_m": 0,
                "source": "gtfs_scheduled",
            }

        if line == "M2":
            m2_calls.append(not_before)

            if len(m2_calls) == 1:
                # Prima interrogazione: serve a scoprire che la
                # banchina metro è a 160 m dalla fermata bus.
                return {
                    "line": "M2",
                    "departure_at": RealDateTime(
                        2026, 9, 10, 7, 51, 0
                    ),
                    "arrival_at": RealDateTime(
                        2026, 9, 10, 7, 56, 0
                    ),
                    "travel_seconds": 5 * 60,
                    "wait_seconds": 1 * 60,
                    "origin_distance_m": 160,
                    "dest_distance_m": 240,
                    "source": "gtfs_scheduled",
                }

            # 160 m / 80 m/min = 2 minuti:
            # siamo realmente pronti per la metro alle 07:52.
            assert not_before == RealDateTime(
                2026, 9, 10, 7, 52, 0
            )

            return {
                "line": "M2",
                "departure_at": RealDateTime(
                    2026, 9, 10, 7, 53, 0
                ),
                "arrival_at": RealDateTime(
                    2026, 9, 10, 7, 58, 0
                ),
                "travel_seconds": 5 * 60,
                "wait_seconds": 1 * 60,
                "origin_distance_m": 160,
                "dest_distance_m": 240,
                "source": "gtfs_scheduled",
            }

        return None

    monkeypatch.setattr(
        atm,
        "_gtfs_next_departure",
        fake_gtfs,
    )

    options = atm._atm_one_transfer_options(
        origin[0],
        origin[1],
        destination[0],
        destination[1],
        ["73"],
        now=now,
    )

    assert options
    best = options[0]

    assert m2_calls == [
        RealDateTime(2026, 9, 10, 7, 50, 0),
        RealDateTime(2026, 9, 10, 7, 52, 0),
    ]

    assert best["transfer_walk_m"] == 160
    assert best["transfer_walk_seconds"] == 2 * 60
    assert best["metro_ready_at"] == RealDateTime(
        2026, 9, 10, 7, 52, 0
    )

    assert best["second_departure_at"] == RealDateTime(
        2026, 9, 10, 7, 53, 0
    )

    assert best["second_arrival_at"] == RealDateTime(
        2026, 9, 10, 7, 58, 0
    )

    # 240 m / 80 m/min = 3 minuti finali.
    assert best["final_walk_m"] == 240
    assert best["final_walk_seconds"] == 3 * 60

    assert best["destination_arrival_at"] == RealDateTime(
        2026, 9, 10, 8, 1, 0
    )

    assert best["eta_seconds"] == 21 * 60


# ROUTE_RANKING_TESTS_START

def test_trip_plan_exposes_numeric_eta_seconds(monkeypatch):
    monkeypatch.setattr(atm, "datetime", FixedDateTime)

    monkeypatch.setattr(
        atm,
        "_browser_fetch_json",
        lambda _path: {
            "Trips": [
                transit_trip(),
            ]
        },
    )

    monkeypatch.setattr(
        atm,
        "_atm_live_minutes",
        lambda *_args, **_kwargs: {
            "status": "ok",
            "arrivals": {
                "44": "5 min",
            },
        },
    )

    result = atm._atm_trip_plan(
        45.507373,
        9.236441,
        45.503443,
        9.220793,
        "destinazione",
    )

    assert result is not None

    # 27 minuti strutturali + 5 minuti di attesa live.
    assert result["summary"]["eta_seconds"] == 32 * 60


def _official_candidate(eta_seconds):
    return {
        "raw": {},
        "summary": {
            "lines": ["73"],
            "first_line": "73",
            "eta_seconds": eta_seconds,
        },
    }


def test_route_ranking_transfer_can_beat_official():
    official = _official_candidate(20 * 60)

    transfer = {
        "first_line": "81",
        "second_line": "M3",
        "eta_seconds": 18 * 60,
    }

    ranked = atm._rank_atm_route_candidates(
        official,
        [transfer],
    )

    assert len(ranked) == 2

    assert ranked[0]["kind"] == "one_transfer"
    assert ranked[0]["eta_seconds"] == 18 * 60
    assert ranked[0]["option"] is transfer

    assert ranked[1]["kind"] == "official_atm_trip"
    assert ranked[1]["eta_seconds"] == 20 * 60


def test_route_ranking_official_can_beat_transfer():
    official = _official_candidate(15 * 60)

    transfer = {
        "first_line": "81",
        "second_line": "M3",
        "eta_seconds": 18 * 60,
    }

    ranked = atm._rank_atm_route_candidates(
        official,
        [transfer],
    )

    assert len(ranked) == 2

    assert ranked[0]["kind"] == "official_atm_trip"
    assert ranked[0]["eta_seconds"] == 15 * 60

    assert ranked[1]["kind"] == "one_transfer"
    assert ranked[1]["eta_seconds"] == 18 * 60


# BUILD_PLAN_RANKING_TESTS_START

def _build_plan_destination():
    return {
        "name": "destinazione-test",
        "label": "Destinazione test",
        "lat": 45.501000,
        "lon": 9.201000,
        "source": "saved",
    }


def _build_plan_official(eta_seconds):
    return {
        "raw": {},
        "summary": {
            "duration": "20",
            "lines": ["73"],
            "first_line": "73",
            "live_wait": "5 min",
            "eta_seconds": eta_seconds,
        },
    }


def _build_plan_origin_stop():
    return {
        "name": "Fermata origine",
        "lat": 45.500000,
        "lon": 9.200000,
        "distance_m": 0,
        "atm_stop_code": "40001",
    }


def _build_plan_route_lines(*_args, **_kwargs):
    return [
        {
            "line": "73",
            "route": "bus",
            "name": "73",
            "operator": "ATM",
        },
        {
            "line": "81",
            "route": "bus",
            "name": "81",
            "operator": "ATM",
        },
    ]


def test_build_plan_selects_one_transfer_when_it_arrives_first(monkeypatch):
    official = _build_plan_official(20 * 60)

    transfer = {
        "first_line": "81",
        "second_line": "M3",
        "eta_seconds": 18 * 60,
    }

    monkeypatch.setattr(
        atm,
        "_resolve_destination",
        lambda _name: _build_plan_destination(),
    )

    monkeypatch.setattr(
        atm,
        "_atm_trip_plan",
        lambda *_args, **_kwargs: official,
    )

    monkeypatch.setattr(
        atm,
        "_nearby_stops",
        lambda *_args, **_kwargs: [
            _build_plan_origin_stop(),
        ],
    )

    monkeypatch.setattr(
        atm,
        "_nearby_route_lines",
        _build_plan_route_lines,
    )

    seen_hints = []

    def fake_one_transfer(
        origin_lat,
        origin_lon,
        dest_lat,
        dest_lon,
        line_hints,
        *,
        now=None,
    ):
        seen_hints.extend(line_hints)
        return [transfer]

    monkeypatch.setattr(
        atm,
        "_atm_one_transfer_options",
        fake_one_transfer,
    )

    monkeypatch.setattr(
        atm,
        "_atm_direct_fallback_options",
        lambda *_args, **_kwargs: [],
    )

    result = atm._build_plan_impl(
        45.500000,
        9.200000,
        "destinazione-test",
    )

    assert "81" in seen_hints

    assert result["route_mode"] == "one_transfer"
    assert result["one_transfer_route"] is transfer

    # Manteniamo anche la soluzione GiroMilano come alternativa.
    assert result["official_route"] is official


def test_build_plan_keeps_official_when_it_arrives_first(monkeypatch):
    official = _build_plan_official(15 * 60)

    transfer = {
        "first_line": "81",
        "second_line": "M3",
        "eta_seconds": 18 * 60,
    }

    monkeypatch.setattr(
        atm,
        "_resolve_destination",
        lambda _name: _build_plan_destination(),
    )

    monkeypatch.setattr(
        atm,
        "_atm_trip_plan",
        lambda *_args, **_kwargs: official,
    )

    monkeypatch.setattr(
        atm,
        "_nearby_stops",
        lambda *_args, **_kwargs: [
            _build_plan_origin_stop(),
        ],
    )

    monkeypatch.setattr(
        atm,
        "_nearby_route_lines",
        _build_plan_route_lines,
    )

    monkeypatch.setattr(
        atm,
        "_atm_one_transfer_options",
        lambda *_args, **_kwargs: [transfer],
    )

    result = atm._build_plan_impl(
        45.500000,
        9.200000,
        "destinazione-test",
    )

    assert result["route_mode"] == "official_atm_trip"
    assert result["official_route"] is official



# LOCAL_ATM_REALTIME_INTEGRATION_TESTS_START

def test_build_plan_prefers_local_atm_realtime_before_legacy_planners(
    monkeypatch,
):
    local_route = {
        "status": "ok",
        "service_date": 20260910,
        "query_departure_s": 38774,
        "arrival_s": 40018,
        "total_seconds": 1244,
        "origin_stop": "via p.te nuovo via biumi",
        "origin_stop_id": "12789",
        "origin_walk_seconds": 119,
        "destination_stop": "GORLA",
        "destination_stop_id": "GORLA",
        "final_walk_seconds": 298,
        "legs": [
            {
                "mode": "transit",
                "route": "51",
                "live": True,
                "live_wait_seconds": 418,
                "from": "via p.te nuovo via biumi",
                "from_stop_id": "12789",
                "to": "precotto m1",
                "to_stop_id": "12567",
                "departure_s": 39192,
                "arrival_s": 39533,
            },
            {
                "mode": "walk",
                "from": "precotto m1",
                "to": "PRECOTTO",
                "walk_seconds": 49,
            },
            {
                "mode": "transit",
                "route": "M1",
                "live": False,
                "from": "PRECOTTO",
                "from_stop_id": "PRECOTTO",
                "to": "GORLA",
                "to_stop_id": "GORLA",
                "departure_s": 39638,
                "arrival_s": 39720,
            },
        ],
    }

    monkeypatch.setattr(
        atm,
        "_resolve_destination",
        lambda _name: _build_plan_destination(),
    )

    monkeypatch.setattr(
        atm,
        "_local_atm_realtime_route",
        lambda *_args, **_kwargs: local_route,
    )

    def legacy_must_not_run(*_args, **_kwargs):
        raise AssertionError(
            "GiroMilano legacy non deve essere interrogato "
            "quando il router locale realtime ha una rotta"
        )

    monkeypatch.setattr(
        atm,
        "_atm_trip_plan",
        legacy_must_not_run,
    )

    result = atm._build_plan_impl(
        45.500000,
        9.200000,
        "destinazione-test",
    )

    assert result["route_mode"] == "local_atm_realtime"
    assert result["local_atm_route"] is local_route

    assert result["destination"]["name"] == "destinazione-test"
    assert result["needs_official_route_lookup"] is False

    assert result["source"] == (
        "ATM realtime + router locale GTFS"
    )


def test_render_reply_local_atm_realtime_route():
    plan = {
        "route_mode": "local_atm_realtime",
        "destination": {
            "name": "sonia",
            "label": "Sonia",
        },
        "local_atm_route": {
            "status": "ok",
            "service_date": 20260910,
            "query_departure_s": 38774,
            "arrival_s": 40018,
            "total_seconds": 1244,
            "origin_stop": "via p.te nuovo via biumi",
            "origin_stop_id": "12789",
            "origin_walk_seconds": 119,
            "destination_stop": "GORLA",
            "destination_stop_id": "GORLA",
            "final_walk_seconds": 298,
            "legs": [
                {
                    "mode": "transit",
                    "route": "51",
                    "live": True,
                    "live_wait_seconds": 418,
                    "from": "via p.te nuovo via biumi",
                    "from_stop_id": "12789",
                    "to": "precotto m1",
                    "to_stop_id": "12567",
                    "departure_s": 39192,
                    "arrival_s": 39533,
                },
                {
                    "mode": "walk",
                    "from": "precotto m1",
                    "to": "PRECOTTO",
                    "walk_seconds": 49,
                },
                {
                    "mode": "transit",
                    "route": "M1",
                    "live": False,
                    "from": "PRECOTTO",
                    "from_stop_id": "PRECOTTO",
                    "to": "GORLA",
                    "to_stop_id": "GORLA",
                    "departure_s": 39638,
                    "arrival_s": 39720,
                },
            ],
        },
        "official_route_url": "https://example.test/atm",
    }

    text = atm.render_reply(plan)

    assert "Sonia" in text

    assert "51" in text
    assert "M1" in text

    assert "via p.te nuovo via biumi" in text.lower()
    assert "precotto" in text.lower()
    assert "gorla" in text.lower()

    # 418 s ~= 7 minuti: il primo boarding usa il realtime.
    assert "attesa live" in text.lower()
    assert "7 min" in text.lower()

    # Un tratto senza realtime deve dichiararlo esplicitamente;
    # l'orario GTFS non va presentato come live.
    assert "attesa live non disponibile" in text.lower()
    assert "m1: orario gtfs programmato" in text.lower()

    # 119 s ~= 2 minuti per raggiungere la prima fermata.
    assert "2 min" in text.lower()

    # 1244 s ~= 21 minuti complessivi.
    assert "21 min" in text.lower()

    # Non deve cadere nel renderer legacy GiroMilano.
    assert "Prendi: 73" not in text


# LOCAL_ATM_REALTIME_INTEGRATION_TESTS_END


# DIRECT_FALLBACK_ETA_TESTS_START

def test_direct_fallback_exposes_complete_eta(monkeypatch):
    monkeypatch.setattr(atm, "datetime", FixedDateTime)

    origin = (45.500000, 9.200000)

    # La fermata di discesa è circa 160 m oltre la destinazione reale.
    destination = (45.508560, 9.200000)

    board = _pattern_stop(
        "50001",
        "Fermata origine",
        45.500000,
        9.200000,
    )

    alight = _pattern_stop(
        "50002",
        "Fermata destinazione",
        45.510000,
        9.200000,
    )

    monkeypatch.setattr(
        atm,
        "_atm_journey_pattern_stops",
        lambda jp: (
            [board, alight]
            if jp == "73|0"
            else []
        ),
    )

    def fake_browser(path):
        if path == "tpl/stops/50001/linesummary":
            return {
                "Lines": [
                    {
                        "Line": {
                            "LineCode": "73",
                            "LineId": "73",
                        },
                        "JourneyPatternId": "73|0",
                        "WaitMessage": "2 min",
                    },
                ]
            }

        return None

    monkeypatch.setattr(
        atm,
        "_browser_fetch_json",
        fake_browser,
    )

    gtfs_calls = []

    def fake_gtfs(
        line,
        origin_lat,
        origin_lon,
        dest_lat,
        dest_lon,
        not_before,
        *,
        gtfs_path=None,
    ):
        gtfs_calls.append((line, not_before))

        assert line == "73"

        return {
            "line": "73",
            # Per il diretto il GTFS fornisce il tempo di marcia.
            # Il live ATM determina invece la partenza effettiva.
            "departure_at": RealDateTime(
                2026, 9, 10, 7, 45, 0
            ),
            "arrival_at": RealDateTime(
                2026, 9, 10, 7, 53, 0
            ),
            "travel_seconds": 8 * 60,
            "wait_seconds": 5 * 60,
            "source": "gtfs_scheduled",
        }

    monkeypatch.setattr(
        atm,
        "_gtfs_next_departure",
        fake_gtfs,
    )

    options = atm._atm_direct_fallback_options(
        origin[0],
        origin[1],
        destination[0],
        destination[1],
        ["73"],
    )

    assert options
    best = options[0]

    assert best["line"] == "73"
    assert best["wait"] == "2 min"
    assert best["wait_source"] == "atm_live"

    assert best["origin_walk_seconds"] == 0
    assert best["departure_at"] == RealDateTime(
        2026, 9, 10, 7, 42, 0
    )

    assert best["travel_seconds"] == 8 * 60

    # ~160 m / 80 m/min -> 2 minuti finali.
    assert 140 <= best["dest_distance_m"] <= 180
    assert best["final_walk_seconds"] == 2 * 60

    assert best["destination_arrival_at"] == RealDateTime(
        2026, 9, 10, 7, 52, 0
    )

    # 2 attesa + 8 viaggio + 2 cammino finale.
    assert best["eta_seconds"] == 12 * 60

    assert gtfs_calls == [
        (
            "73",
            RealDateTime(2026, 9, 10, 7, 40, 0),
        )
    ]


def test_route_ranking_direct_can_beat_official_and_transfer():
    official = _official_candidate(20 * 60)

    transfer = {
        "first_line": "81",
        "second_line": "M3",
        "eta_seconds": 18 * 60,
    }

    direct = {
        "line": "73",
        "eta_seconds": 16 * 60,
    }

    ranked = atm._rank_atm_route_candidates(
        official,
        [transfer],
        direct_options=[direct],
    )

    assert len(ranked) == 3

    assert ranked[0]["kind"] == "direct_atm"
    assert ranked[0]["eta_seconds"] == 16 * 60
    assert ranked[0]["option"] is direct

    assert ranked[1]["kind"] == "one_transfer"
    assert ranked[1]["eta_seconds"] == 18 * 60

    assert ranked[2]["kind"] == "official_atm_trip"
    assert ranked[2]["eta_seconds"] == 20 * 60


def test_build_plan_selects_direct_when_it_arrives_first(monkeypatch):
    official = _build_plan_official(20 * 60)

    transfer = {
        "first_line": "81",
        "second_line": "M3",
        "eta_seconds": 18 * 60,
    }

    direct = {
        "line": "73",
        "eta_seconds": 16 * 60,
    }

    monkeypatch.setattr(
        atm,
        "_resolve_destination",
        lambda _name: _build_plan_destination(),
    )

    monkeypatch.setattr(
        atm,
        "_atm_trip_plan",
        lambda *_args, **_kwargs: official,
    )

    monkeypatch.setattr(
        atm,
        "_nearby_stops",
        lambda *_args, **_kwargs: [
            _build_plan_origin_stop(),
        ],
    )

    monkeypatch.setattr(
        atm,
        "_nearby_route_lines",
        _build_plan_route_lines,
    )

    monkeypatch.setattr(
        atm,
        "_atm_one_transfer_options",
        lambda *_args, **_kwargs: [transfer],
    )

    direct_calls = []

    def fake_direct(
        origin_lat,
        origin_lon,
        dest_lat,
        dest_lon,
        line_hints,
    ):
        direct_calls.append(list(line_hints))
        return [direct]

    monkeypatch.setattr(
        atm,
        "_atm_direct_fallback_options",
        fake_direct,
    )

    result = atm._build_plan_impl(
        45.500000,
        9.200000,
        "destinazione-test",
    )

    assert direct_calls
    assert "73" in direct_calls[0]

    assert result["route_mode"] == "direct_atm"
    assert result["direct_atm_route"] is direct

    # Conserviamo le altre alternative per spiegazione/debug.
    assert result["official_route"] is official
    assert result["one_transfer_options"] == [transfer]


# RENDER_SELECTED_ROUTE_TESTS_START

def test_render_reply_respects_direct_atm_winner():
    official = _build_plan_official(20 * 60)

    plan = {
        "route_mode": "direct_atm",
        "destination": {
            "name": "dest",
            "label": "Destinazione test",
        },
        "direct_atm_route": {
            "line": "81",
            "wait": "2 min",
            "wait_source": "atm_live",
            "origin_stop_name": "Fermata A",
            "dest_stop_name": "Fermata B",
            "stops_count": 3,
            "origin_distance_m": 0,
            "dest_distance_m": 160,
            "final_walk_m": 160,
            "departure_at": RealDateTime(
                2026, 9, 10, 7, 42, 0
            ),
            "destination_arrival_at": RealDateTime(
                2026, 9, 10, 7, 52, 0
            ),
            "eta_seconds": 12 * 60,
        },
        "official_route": official,
        "official_route_url": "https://example.test/atm",
    }

    text = atm.render_reply(plan)

    assert "81" in text
    assert "2 min" in text
    assert "Fermata A" in text
    assert "Fermata B" in text
    assert "07:52" in text

    # Non deve ricadere nel renderer dell'itinerario ufficiale.
    assert "Prendi: 73" not in text


def test_render_reply_respects_one_transfer_winner():
    official = _build_plan_official(25 * 60)

    plan = {
        "route_mode": "one_transfer",
        "destination": {
            "name": "dest",
            "label": "Destinazione test",
        },
        "one_transfer_route": {
            "first_line": "81",
            "second_line": "M3",
            "origin_stop_name": "Fermata A",
            "transfer_stop_name": "Cambio Test",
            "first_wait": "in arrivo",
            "first_wait_source": "atm_live",
            "transfer_walk_m": 120,
            "second_wait_seconds": 2 * 60,
            "second_wait_source": "gtfs_scheduled",
            "final_walk_m": 240,
            "destination_arrival_at": RealDateTime(
                2026, 9, 10, 8, 0, 0
            ),
            "eta_seconds": 20 * 60,
        },
        "official_route": official,
        "official_route_url": "https://example.test/atm",
    }

    text = atm.render_reply(plan)

    assert "81" in text
    assert "M3" in text
    assert "Cambio Test" in text
    assert "in arrivo" in text.lower()
    assert "08:00" in text

    # Anche qui l'alternativa GiroMilano non deve sovrascrivere
    # il vincitore del ranking.
    assert "Prendi: 73" not in text


# GTFS_SUBWAY_NEARBY_TESTS_START

def test_gtfs_subway_lines_near_uses_only_subway_routes(tmp_path):
    import zipfile

    gtfs = tmp_path / "gtfs.zip"

    files = {
        "routes.txt": (
            "route_id,route_short_name,route_long_name,route_type\n"
            "M1,1,M1 - Sesto FS - Rho Fiera,1\n"
            "BUS51,51,51 - Cimiano - Istria,3\n"
        ),
        "trips.txt": (
            "route_id,service_id,trip_id\n"
            "M1,SVC,M1_TRIP\n"
            "BUS51,SVC,BUS_TRIP\n"
        ),
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "METRO_STOP,Precotto M1,45.512393,9.225171\n"
            "BUS_STOP,Precotto superficie,45.512400,9.225180\n"
        ),
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "M1_TRIP,07:40:00,07:40:00,METRO_STOP,1\n"
            "BUS_TRIP,07:40:00,07:40:00,BUS_STOP,1\n"
        ),
    }

    with zipfile.ZipFile(gtfs, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)

    lines = atm._gtfs_subway_lines_near(
        45.512393,
        9.225171,
        radius_m=150,
        gtfs_path=gtfs,
    )

    assert lines == ["M1"]

    far = atm._gtfs_subway_lines_near(
        45.520000,
        9.240000,
        radius_m=150,
        gtfs_path=gtfs,
    )

    assert far == []


# GTFS_LINE_SCHEDULE_INDEX_TESTS_START

def test_gtfs_line_schedule_index_batches_only_requested_lines(tmp_path):
    import zipfile

    gtfs = tmp_path / "gtfs.zip"

    files = {
        "routes.txt": (
            "route_id,route_short_name,route_long_name,route_type\n"
            "R73,73,73 bus,3\n"
            "RM2,2,M2 metro,1\n"
            "R51,51,51 bus,3\n"
        ),
        "trips.txt": (
            "route_id,service_id,trip_id,trip_headsign\n"
            "R73,SVC,T73,Test 73\n"
            "RM2,SVC,TM2,Test M2\n"
            "R51,SVC,T51,Test 51\n"
        ),
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "A,Fermata A,45.5000,9.2000\n"
            "B,Fermata B,45.5010,9.2010\n"
            "C,Fermata C,45.5020,9.2020\n"
            "D,Fermata D,45.5030,9.2030\n"
        ),
        "stop_times.txt": (
            "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
            "T73,07:40:00,07:40:00,A,1\n"
            "T73,07:48:00,07:48:00,B,2\n"
            "TM2,07:50:00,07:50:00,B,1\n"
            "TM2,07:55:00,07:55:00,C,2\n"
            "T51,07:41:00,07:41:00,C,1\n"
            "T51,07:49:00,07:49:00,D,2\n"
        ),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,"
            "friday,saturday,sunday,start_date,end_date\n"
            "SVC,1,1,1,1,1,1,1,20260101,20261231\n"
        ),
        "calendar_dates.txt": (
            "service_id,date,exception_type\n"
        ),
    }

    with zipfile.ZipFile(gtfs, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)

    index = atm._gtfs_line_schedule_index(
        ["73", "M2"],
        gtfs_path=gtfs,
    )

    assert set(index) == {"73", "M2"}

    assert set(index["73"]["trip_meta"]) == {"T73"}
    assert set(index["M2"]["trip_meta"]) == {"TM2"}

    assert len(
        index["73"]["trip_stop_times"]["T73"]
    ) == 2

    assert len(
        index["M2"]["trip_stop_times"]["TM2"]
    ) == 2

    # La linea non richiesta non deve occupare memoria nella cache.
    assert all(
        "T51" not in data["trip_meta"]
        for data in index.values()
    )


# LOCAL_ATM_SECOND_BOARDING_REALTIME_TEST_START

def test_local_atm_realtime_refreshes_second_boarding_stop(
    monkeypatch,
    tmp_path,
):
    router_bin = tmp_path / "atm-router"
    router_bin.write_text("", encoding="utf-8")

    graph = tmp_path / "graph.bin"
    graph.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        atm,
        "ATM_LOCAL_ROUTER_BIN",
        router_bin,
    )
    monkeypatch.setattr(
        atm,
        "ATM_LOCAL_ROUTER_GRAPH",
        graph,
    )

    browser_paths = []
    route_calls = []

    def fake_browser(path):
        browser_paths.append(path)

        if path == "tpl/stops/A/linesummary":
            return {
                "Lines": [
                    {
                        "Line": {
                            "LineCode": "R1",
                            "LineId": "R1",
                        },
                        "Direction": "0",
                        "WaitMessage": "1 min",
                    }
                ]
            }

        if path == "tpl/stops/C/linesummary":
            return {
                "Lines": [
                    {
                        "Line": {
                            "LineCode": "R2",
                            "LineId": "R2",
                        },
                        "Direction": "0",
                        "WaitMessage": "10 min",
                    }
                ]
            }

        return {"Lines": []}

    monkeypatch.setattr(
        atm,
        "_browser_fetch_json",
        fake_browser,
    )

    first_route = {
        "status": "ok",
        "service_date": int(
            atm.datetime.now().strftime("%Y%m%d")
        ),
        "query_departure_s": 36000,
        "arrival_s": 36600,
        "total_seconds": 600,
        "origin_stop": "Stop A",
        "origin_stop_id": "A",
        "origin_walk_seconds": 0,
        "destination_stop": "Stop D",
        "destination_stop_id": "D",
        "final_walk_seconds": 0,
        "legs": [
            {
                "mode": "transit",
                "route": "R1",
                "live": True,
                "live_wait_seconds": 60,
                "from": "Stop A",
                "from_stop_id": "A",
                "to": "Stop B",
                "to_stop_id": "B",
                "departure_s": 36060,
                "arrival_s": 36120,
            },
            {
                "mode": "walk",
                "from": "Stop B",
                "to": "Stop C",
                "walk_seconds": 60,
            },
            {
                "mode": "transit",
                "route": "R2",
                "live": False,
                "from": "Stop C",
                "from_stop_id": "C",
                "to": "Stop D",
                "to_stop_id": "D",
                "departure_s": 36200,
                "arrival_s": 36600,
            },
        ],
    }

    second_route = {
        **first_route,
        "arrival_s": 37300,
        "total_seconds": 1300,
        "legs": [
            first_route["legs"][0],
            first_route["legs"][1],
            {
                **first_route["legs"][2],
                "live": True,
                "live_wait_seconds": 600,
                "departure_s": 37200,
                "arrival_s": 37300,
            },
        ],
    }

    def fake_router(args, *, timeout_s=3.0):
        if args[0] == "--nearby":
            return {
                "status": "ok",
                "service_date": int(
                    atm.datetime.now().strftime("%Y%m%d")
                ),
                "stops": [
                    {
                        "stop_id": "A",
                        "name": "Stop A",
                        "distance_m": 10,
                        "walk_seconds": 8,
                    }
                ],
            }

        assert args[0] == "--route"
        route_calls.append(list(args))

        if len(route_calls) == 1:
            return first_route

        flat = " ".join(args)

        assert "--live C R2 0 " in flat
        return second_route

    monkeypatch.setattr(
        atm,
        "_local_atm_router_json",
        fake_router,
    )

    result = atm._local_atm_realtime_route(
        45.000000,
        9.000000,
        45.020000,
        9.000000,
    )

    assert "tpl/stops/C/linesummary" in browser_paths
    assert len(route_calls) == 2

    transit = [
        leg
        for leg in result["legs"]
        if leg["mode"] == "transit"
    ]

    assert transit[1]["route"] == "R2"
    assert transit[1]["live"] is True


# LOCAL_ATM_SECOND_BOARDING_REALTIME_TEST_END
