import json
import struct
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools" / "atm_router" / "atm_router.c"

HEADER = struct.Struct("<8I6Q")
STOP = struct.Struct("<iiII")
ROUTE = struct.Struct("<IIHH")
CONNECTION = struct.Struct("<7I")
TRANSFER = struct.Struct("<IIHH")

MAGIC = 0x4D544152
VERSION = 3


@pytest.fixture
def router_bin(tmp_path):
    binary = tmp_path / "atm-router"

    subprocess.run(
        [
            "cc",
            "-O2",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-pedantic",
            "-o",
            str(binary),
            str(SOURCE),
            "-lm",
        ],
        check=True,
    )

    return binary


def _write_transfer_graph(path):
    strings = bytearray()
    offsets = {}

    def add_string(value):
        if value in offsets:
            return offsets[value]

        offset = len(strings)
        offsets[value] = offset
        strings.extend(value.encode("utf-8"))
        strings.append(0)
        return offset

    # A è l'origine.
    # B -> C è il cambio pedonale.
    # D è la destinazione.
    #
    # C è >900 m dall'origine, quindi il live R2 non può essere
    # seminato direttamente come "prima salita".
    #
    # D è >900 m da B/C, quindi R2 è necessario.
    stops = [
        (
            int(45.0000000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("Stop A"),
            add_string("A"),
        ),
        (
            int(45.0100000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("Stop B"),
            add_string("B"),
        ),
        (
            int(45.0101000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("Stop C"),
            add_string("C"),
        ),
        (
            int(45.0200000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("Stop D"),
            add_string("D"),
        ),
    ]

    routes = [
        (
            add_string("R1"),
            add_string("route-R1"),
            3,
            0,
        ),
        (
            add_string("R2"),
            add_string("route-R2"),
            3,
            0,
        ),
    ]

    # Query 10:00:00 = 36000.
    #
    # R1 live: +60 s, uguale al GTFS.
    #
    # R2 GTFS: 10:03:20.
    # R2 LIVE: +1200 s dalla query = 10:20:00.
    #
    # Il router NON deve prendere R2 alle 10:03:20 dopo il cambio.
    connections = [
        (
            0,       # A
            1,       # B
            0,       # R1
            100,     # trip
            36060,
            36120,
            0,       # direction 0
        ),
        (
            2,       # C
            3,       # D
            1,       # R2
            200,     # trip
            36200,
            36300,
            0,       # direction 0
        ),
    ]

    transfers = [
        (
            1,       # B
            2,       # C
            60,      # walk seconds
            0,
        ),
    ]

    header_size = HEADER.size
    stops_offset = header_size
    routes_offset = (
        stops_offset
        + len(stops) * STOP.size
    )
    connections_offset = (
        routes_offset
        + len(routes) * ROUTE.size
    )
    transfers_offset = (
        connections_offset
        + len(connections) * CONNECTION.size
    )
    strings_offset = (
        transfers_offset
        + len(transfers) * TRANSFER.size
    )

    header = HEADER.pack(
        MAGIC,
        VERSION,
        20260910,
        0,
        len(stops),
        len(routes),
        len(connections),
        len(transfers),
        stops_offset,
        routes_offset,
        connections_offset,
        transfers_offset,
        strings_offset,
        len(strings),
    )

    with path.open("wb") as f:
        f.write(header)

        for row in stops:
            f.write(STOP.pack(*row))

        for row in routes:
            f.write(ROUTE.pack(*row))

        for row in connections:
            f.write(CONNECTION.pack(*row))

        for row in transfers:
            f.write(TRANSFER.pack(*row))

        f.write(strings)


def test_realtime_is_used_after_walking_transfer(
    router_bin,
    tmp_path,
):
    graph = tmp_path / "graph.bin"
    _write_transfer_graph(graph)

    completed = subprocess.run(
        [
            str(router_bin),
            "--route",
            str(graph),
            "45.0000000",
            "9.0000000",
            "45.0200000",
            "9.0000000",
            "10:00:00",
            "--live",
            "A",
            "R1",
            "0",
            "60",
            "--live",
            "C",
            "R2",
            "0",
            "1200",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    result = json.loads(completed.stdout)

    assert result["status"] == "ok"

    transit = [
        leg
        for leg in result["legs"]
        if leg["mode"] == "transit"
    ]

    assert len(transit) == 2

    first, second = transit

    assert first["route"] == "R1"
    assert first["live"] is True
    assert first["departure_s"] == 36060

    assert second["route"] == "R2"

    # Regressione: dopo un cambio a piedi il router non deve
    # usare la vecchia partenza GTFS 36200 ignorando il realtime.
    assert second["live"] is True
    assert second["departure_s"] == 37200
    assert second["arrival_s"] == 37300
