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


def _write_short_route_choice_graph(path):
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

    # Origine: 45.000000, 9.000000
    # Destinazione: 45.000000, 9.004000
    #
    # BAD:
    # bisogna quasi raggiungere la destinazione a piedi per
    # prendere il mezzo, quindi accesso + uscita superano il
    # cammino diretto.
    #
    # GOOD:
    # fermata vicina all'origine e fermata vicina alla
    # destinazione. Arriva leggermente dopo BAD, così prima
    # della regressione BAD vinceva esclusivamente per ETA.
    stops = [
        (
            int(45.0000000 * 10_000_000),
            int(9.0005000 * 10_000_000),
            add_string("Good origin"),
            add_string("GOOD-A"),
        ),
        (
            int(45.0000000 * 10_000_000),
            int(9.0035000 * 10_000_000),
            add_string("Good destination"),
            add_string("GOOD-B"),
        ),
        (
            int(45.0000000 * 10_000_000),
            int(9.0045000 * 10_000_000),
            add_string("Bad origin"),
            add_string("BAD-A"),
        ),
        (
            int(45.0000000 * 10_000_000),
            int(9.0049000 * 10_000_000),
            add_string("Bad destination"),
            add_string("BAD-B"),
        ),
    ]

    routes = [
        (
            add_string("GOOD"),
            add_string("route-GOOD"),
            3,
            0,
        ),
        (
            add_string("BAD"),
            add_string("route-BAD"),
            3,
            0,
        ),
    ]

    # Query 10:00.
    #
    # GOOD parte prima ma arriva alle 10:06.
    # BAD parte alle 10:04:30 e arriva alle 10:05:
    # senza il filtro sul cammino totale BAD risulta il più
    # veloce, nonostante sia un evidente detour pedonale.
    #
    # Le connessioni restano ordinate per departure_s.
    connections = [
        (
            0,       # GOOD-A
            1,       # GOOD-B
            0,       # GOOD
            100,
            36120,   # 10:02:00
            36360,   # 10:06:00
            0,
        ),
        (
            2,       # BAD-A
            3,       # BAD-B
            1,       # BAD
            200,
            36270,   # 10:04:30
            36300,   # 10:05:00
            0,
        ),
    ]

    transfers = []

    header_size = HEADER.size
    stops_offset = header_size
    routes_offset = stops_offset + len(stops) * STOP.size
    connections_offset = routes_offset + len(routes) * ROUTE.size
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


def test_transit_detour_with_more_walking_than_direct_is_rejected(
    router_bin,
    tmp_path,
):
    graph = tmp_path / "short-choice.bin"
    _write_short_route_choice_graph(graph)

    completed = subprocess.run(
        [
            str(router_bin),
            "--route",
            str(graph),
            "45.0000000",
            "9.0000000",
            "45.0000000",
            "9.0040000",
            "10:00:00",
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

    assert len(transit) == 1
    assert transit[0]["route"] == "GOOD"

    # Il candidato scelto deve richiedere meno cammino
    # dell'intero tragitto diretto.
    assert (
        result["origin_walk_seconds"]
        + result["final_walk_seconds"]
        < 240
    )
def _write_subway_with_surface_live_graph(path):
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

    # A = origine, D = destinazione.
    #
    # M1 (route_type 1) è la scelta più rapida e usa solo GTFS.
    # B1 è una linea di superficie con realtime.
    #
    # La presenza del live B1 non deve eliminare M1 dalla prima
    # salita soltanto perché M1 non possiede un --live.
    stops = [
        (
            int(45.0000000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("Origin"),
            add_string("A"),
        ),
        (
            int(45.0200000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("Destination"),
            add_string("D"),
        ),
    ]

    routes = [
        (
            add_string("M1"),
            add_string("route-M1"),
            1,
            0,
        ),
        (
            add_string("B1"),
            add_string("route-B1"),
            3,
            0,
        ),
    ]

    connections = [
        (
            0,
            1,
            0,
            100,
            36060,
            36300,
            0,
        ),
        (
            0,
            1,
            1,
            200,
            36060,
            37200,
            0,
        ),
    ]

    transfers = []

    header_size = HEADER.size
    stops_offset = header_size
    routes_offset = stops_offset + len(stops) * STOP.size
    connections_offset = routes_offset + len(routes) * ROUTE.size
    transfers_offset = connections_offset + len(connections) * CONNECTION.size
    strings_offset = transfers_offset + len(transfers) * TRANSFER.size

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


def test_subway_gtfs_remains_available_when_surface_live_exists(
    router_bin,
    tmp_path,
):
    graph = tmp_path / "subway-live-competition.bin"
    _write_subway_with_surface_live_graph(graph)

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
            "B1",
            "0",
            "120",
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

    assert len(transit) == 1
    assert transit[0]["route"] == "M1"
    assert transit[0]["live"] is False
    assert transit[0]["from_stop_id"] == "A"
    assert transit[0]["to_stop_id"] == "D"
    assert transit[0]["departure_s"] == 36060
    assert transit[0]["arrival_s"] == 36300


def _write_equal_arrival_walk_vs_board_graph(path):
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

    # A è esattamente l'origine.
    # B è circa 80 metri dopo A: ~60 secondi a piedi.
    # È lo STESSO treno M1:
    #
    # A 10:05 -> B 10:06 -> D 10:10
    #
    # Due possibilità con identico arrivo:
    #
    # 1. aspettare M1 ad A -> D 10:10, zero cammino;
    # 2. camminare A->B -> prendere lo stesso M1 -> D 10:10.
    #
    # A parità di arrival time deve vincere A.

    stops = [
        (
            int(45.0000000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("STAZIONE A"),
            add_string("A"),
        ),
        (
            int(45.0007200 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("STAZIONE B"),
            add_string("B"),
        ),
        (
            int(45.0100000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("DESTINAZIONE"),
            add_string("D"),
        ),
    ]

    routes = [
        (
            add_string("M1"),
            add_string("route-M1"),
            1,
            0,
        ),
    ]

    connections = [
        (
            0,      # A
            1,      # B
            0,      # M1
            100,    # stesso trip
            36300,  # 10:05
            36360,  # 10:06
            0,
        ),
        (
            1,      # B
            2,      # D
            0,      # M1
            100,    # stesso trip
            36360,  # 10:06
            36600,  # 10:10
            0,
        ),
    ]

    transfers = []

    header_size = HEADER.size
    stops_offset = header_size
    routes_offset = stops_offset + len(stops) * STOP.size
    connections_offset = routes_offset + len(routes) * ROUTE.size
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


def test_equal_arrival_prefers_less_initial_walking(
    router_bin,
    tmp_path,
):
    graph = tmp_path / "equal-arrival.bin"
    _write_equal_arrival_walk_vs_board_graph(graph)

    completed = subprocess.run(
        [
            str(router_bin),
            "--route",
            str(graph),
            "45.0000000",
            "9.0000000",
            "45.0100000",
            "9.0000000",
            "10:00:00",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    result = json.loads(completed.stdout)

    assert result["status"] == "ok"
    assert result["arrival_s"] == 36600

    # A e B portano sullo stesso M1 e arrivano entrambi alle 10:10.
    # Deve vincere A perché non richiede cammino iniziale.
    assert result["origin_stop_id"] == "A"
    assert result["origin_walk_seconds"] == 0

    transit = [
        leg
        for leg in result["legs"]
        if leg["mode"] == "transit"
    ]

    assert len(transit) == 1
    assert transit[0]["route"] == "M1"
    assert transit[0]["from_stop_id"] == "A"
    assert transit[0]["to_stop_id"] == "D"



def _write_boarding_tiebreak_graph(
    path,
    *,
    alternative_arrival_s=36500,
):
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

    # Query: 10:00.
    #
    # Percorso A:
    #   origine A -> R1 -> R2 -> Y
    #   due salite
    #   zero cammino iniziale + ~101 s finale.
    #
    # Percorso X:
    #   ~101 s a piedi fino a X
    #   M1 X -> C -> D
    #   UNA sola salita perché i due archi appartengono allo
    #   stesso trip M1.
    #
    # Con alternative_arrival_s=36500:
    # entrambi arrivano fisicamente a destinazione alle 10:10:01
    # e camminano lo stesso tempo. Deve vincere M1: una salita.
    #
    # Con alternative_arrival_s=36499:
    # R1+R2 arriva un secondo prima e DEVE vincere comunque.

    stops = [
        (
            int(45.0000000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("ORIGINE A"),
            add_string("A"),
        ),
        (
            int(45.0012000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("ORIGINE X"),
            add_string("X"),
        ),
        (
            int(45.0101000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("CAMBIO B"),
            add_string("B"),
        ),
        (
            int(45.0100000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("M1 C"),
            add_string("C"),
        ),
        (
            int(45.0188000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("ARRIVO Y"),
            add_string("Y"),
        ),
        (
            int(45.0200000 * 10_000_000),
            int(9.0000000 * 10_000_000),
            add_string("DESTINAZIONE D"),
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
        (
            add_string("M1"),
            add_string("route-M1"),
            1,
            0,
        ),
    ]

    connections = [
        (
            0,       # A
            2,       # B
            0,       # R1
            100,
            36010,
            36060,
            0,
        ),
        (
            2,       # B
            4,       # Y
            1,       # R2: seconda salita
            200,
            36060,
            alternative_arrival_s,
            0,
        ),
        (
            1,       # X
            3,       # C
            2,       # M1
            300,     # stesso trip...
            36120,
            36300,
            0,
        ),
        (
            3,       # C
            5,       # D
            2,       # M1
            300,     # ...quindi NON nuova salita
            36300,
            36601,
            0,
        ),
    ]

    transfers = []

    header_size = HEADER.size
    stops_offset = header_size
    routes_offset = stops_offset + len(stops) * STOP.size
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


def test_equal_arrival_and_walk_prefers_fewer_boardings(
    router_bin,
    tmp_path,
):
    graph = tmp_path / "boarding-tiebreak.bin"

    _write_boarding_tiebreak_graph(
        graph,
        alternative_arrival_s=36500,
    )

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
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    result = json.loads(completed.stdout)

    assert result["status"] == "ok"
    assert result["arrival_s"] == 36601

    # Stesso arrivo, stesso cammino:
    # deve vincere una sola salita M1.
    assert result["origin_stop_id"] == "X"

    transit = [
        leg
        for leg in result["legs"]
        if leg["mode"] == "transit"
    ]

    assert len(transit) == 1
    assert transit[0]["route"] == "M1"
    assert transit[0]["from_stop_id"] == "X"
    assert transit[0]["to_stop_id"] == "D"


def test_one_second_faster_beats_fewer_boardings(
    router_bin,
    tmp_path,
):
    graph = tmp_path / "boarding-primary-arrival.bin"

    _write_boarding_tiebreak_graph(
        graph,
        alternative_arrival_s=36499,
    )

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
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    result = json.loads(completed.stdout)

    assert result["status"] == "ok"
    assert result["arrival_s"] == 36600

    # R1+R2 ha due salite, ma arriva un secondo prima:
    # deve vincere perché l'ETA resta il criterio assoluto.
    assert result["origin_stop_id"] == "A"

    transit = [
        leg
        for leg in result["legs"]
        if leg["mode"] == "transit"
    ]

    assert [leg["route"] for leg in transit] == ["R1", "R2"]
