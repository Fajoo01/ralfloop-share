#!/usr/bin/env python3

from __future__ import annotations

import argparse
import csv
import io
import math
import struct
import sys
import zipfile

from collections import defaultdict
from datetime import date
from pathlib import Path


MAGIC = 0x4D544152
VERSION = 3

HEADER = struct.Struct("<8I6Q")
STOP = struct.Struct("<iiII")
ROUTE = struct.Struct("<IIHH")
CONNECTION = struct.Struct("<7I")
TRANSFER = struct.Struct("<IIHH")

assert HEADER.size == 80
assert STOP.size == 16
assert ROUTE.size == 12
assert CONNECTION.size == 28
assert TRANSFER.size == 12


def gtfs_rows(zf: zipfile.ZipFile, name: str):
    try:
        raw = zf.open(name)
    except KeyError:
        return

    with raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig")
        yield from csv.DictReader(text)


def gtfs_seconds(value: str | None) -> int | None:
    value = str(value or "").strip()

    if not value:
        return None

    try:
        hh, mm, ss = (int(x) for x in value.split(":"))
    except Exception:
        return None

    value = hh * 3600 + mm * 60 + ss

    if value < 0 or value > 172800:
        return None

    return value


def active_services(
    zf: zipfile.ZipFile,
    service_date: date,
) -> set[str]:
    ymd = service_date.strftime("%Y%m%d")
    weekday = service_date.strftime("%A").lower()

    active: set[str] = set()

    for row in gtfs_rows(zf, "calendar.txt") or []:
        start = str(row.get("start_date") or "")
        end = str(row.get("end_date") or "")

        if not start or not end:
            continue

        if not (start <= ymd <= end):
            continue

        if str(row.get(weekday) or "") != "1":
            continue

        service_id = str(row.get("service_id") or "").strip()

        if service_id:
            active.add(service_id)

    for row in gtfs_rows(zf, "calendar_dates.txt") or []:
        if str(row.get("date") or "") != ymd:
            continue

        service_id = str(row.get("service_id") or "").strip()
        exception_type = str(
            row.get("exception_type") or ""
        ).strip()

        if not service_id:
            continue

        if exception_type == "1":
            active.add(service_id)
        elif exception_type == "2":
            active.discard(service_id)

    return active


def distance_m(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
) -> float:
    r = 6371000.0

    p1 = math.radians(lat1)
    p2 = math.radians(lat2)

    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)

    a = (
        math.sin(dp / 2.0) ** 2
        + math.cos(p1)
        * math.cos(p2)
        * math.sin(dl / 2.0) ** 2
    )

    return 2.0 * r * math.asin(math.sqrt(a))


class Strings:
    def __init__(self):
        self.data = bytearray(b"\0")
        self.offsets = {"": 0}

    def add(self, value: str | None) -> int:
        value = str(value or "")

        old = self.offsets.get(value)
        if old is not None:
            return old

        raw = value.encode("utf-8") + b"\0"
        offset = len(self.data)

        if offset > 0xFFFFFFFF:
            raise RuntimeError("string table oltre 4 GiB")

        self.data.extend(raw)
        self.offsets[value] = offset

        return offset


def canonical_short_name(row: dict[str, str]) -> str:
    short = str(row.get("route_short_name") or "").strip()
    route_id = str(row.get("route_id") or "").strip()
    route_type = str(row.get("route_type") or "").strip()

    if route_type == "1":
        upper = short.upper()

        if upper.startswith("M"):
            return upper

        if short:
            return f"M{short}"

        upper_id = route_id.upper()

        if upper_id.startswith("M"):
            return upper_id

    return short or route_id


def build_transfers(
    stops: list[dict],
    radius_m: int,
    max_neighbors: int,
) -> list[tuple[int, int, int]]:
    if not stops:
        return []

    # ~250 m: griglia abbastanza piccola da evitare O(n²).
    cell_deg = max(radius_m / 111000.0, 0.0001)

    grid: dict[tuple[int, int], list[int]] = defaultdict(list)

    for i, stop in enumerate(stops):
        key = (
            math.floor(stop["lat"] / cell_deg),
            math.floor(stop["lon"] / cell_deg),
        )
        grid[key].append(i)

    transfers: list[tuple[int, int, int]] = []

    for i, stop in enumerate(stops):
        cx = math.floor(stop["lat"] / cell_deg)
        cy = math.floor(stop["lon"] / cell_deg)

        nearby: list[tuple[float, int]] = []

        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in grid.get((cx + dx, cy + dy), []):
                    if i == j:
                        continue

                    other = stops[j]

                    d = distance_m(
                        stop["lat"],
                        stop["lon"],
                        other["lat"],
                        other["lon"],
                    )

                    if d <= radius_m:
                        nearby.append((d, j))

        nearby.sort(key=lambda x: (x[0], x[1]))

        for d, j in nearby[:max_neighbors]:
            # 80 m/min, minimo 30 s per un cambio fisico.
            walk_s = max(
                30,
                int(math.ceil(d / (80.0 / 60.0))),
            )

            walk_s = min(walk_s, 65535)

            transfers.append((i, j, walk_s))

    return transfers


def main() -> int:
    parser = argparse.ArgumentParser()

    parser.add_argument("--gtfs", required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", required=True)

    parser.add_argument(
        "--transfer-radius-m",
        type=int,
        default=250,
    )

    parser.add_argument(
        "--max-transfer-neighbors",
        type=int,
        default=12,
    )

    args = parser.parse_args()

    service_date = date.fromisoformat(args.date)

    gtfs_path = Path(args.gtfs)
    output_path = Path(args.output)

    if not gtfs_path.is_file():
        raise SystemExit(f"GTFS non trovato: {gtfs_path}")

    strings = Strings()

    with zipfile.ZipFile(gtfs_path) as zf:
        services = active_services(zf, service_date)

        print(
            f"active_services={len(services)}",
            file=sys.stderr,
            flush=True,
        )

        if not services:
            raise SystemExit(
                f"nessun servizio attivo per {service_date}"
            )

        # ----------------------------------------------------
        # Fermate: il file è piccolo, lo teniamo interamente.
        # ----------------------------------------------------

        all_stops: dict[str, dict] = {}

        for row in gtfs_rows(zf, "stops.txt") or []:
            stop_id = str(row.get("stop_id") or "").strip()

            if not stop_id:
                continue

            try:
                lat = float(row.get("stop_lat"))
                lon = float(row.get("stop_lon"))
            except Exception:
                continue

            all_stops[stop_id] = {
                "id": stop_id,
                "name": str(row.get("stop_name") or "").strip(),
                "lat": lat,
                "lon": lon,
            }

        print(
            f"all_stops={len(all_stops)}",
            file=sys.stderr,
            flush=True,
        )

        # ----------------------------------------------------
        # Route.
        # ----------------------------------------------------

        route_rows: dict[str, dict] = {}

        for row in gtfs_rows(zf, "routes.txt") or []:
            route_id = str(row.get("route_id") or "").strip()

            if route_id:
                route_rows[route_id] = dict(row)

        # ----------------------------------------------------
        # Solo trip attivi nella giornata.
        # ----------------------------------------------------

        active_trips: dict[str, dict] = {}

        active_route_ids: set[str] = set()

        for row in gtfs_rows(zf, "trips.txt") or []:
            service_id = str(
                row.get("service_id") or ""
            ).strip()

            if service_id not in services:
                continue

            trip_id = str(row.get("trip_id") or "").strip()
            route_id = str(row.get("route_id") or "").strip()

            if not trip_id or not route_id:
                continue

            raw_direction = str(
                row.get("direction_id") or ""
            ).strip()

            if raw_direction == "0":
                direction = 0
            elif raw_direction == "1":
                direction = 1
            else:
                direction = 2

            active_trips[trip_id] = {
                "route_id": route_id,
                "service_id": service_id,
                "direction": direction,
            }

            active_route_ids.add(route_id)

        print(
            f"active_trips={len(active_trips)}",
            file=sys.stderr,
            flush=True,
        )

        if not active_trips:
            raise SystemExit("nessun trip attivo")

        # ----------------------------------------------------
        # Tabelle route nel grafo.
        # ----------------------------------------------------

        route_ids = sorted(active_route_ids)

        route_index = {
            route_id: i
            for i, route_id in enumerate(route_ids)
        }

        graph_routes: list[tuple[int, int, int, int]] = []

        for route_id in route_ids:
            row = route_rows.get(route_id) or {}

            short = canonical_short_name(row)

            try:
                route_type = int(
                    row.get("route_type") or 0
                )
            except Exception:
                route_type = 0

            route_type = max(0, min(route_type, 65535))

            graph_routes.append((
                strings.add(short),
                strings.add(route_id),
                route_type,
                0,
            ))

        # Trip ID serve solo come identità nel router.
        trip_index = {
            trip_id: i
            for i, trip_id in enumerate(active_trips)
        }

        # ----------------------------------------------------
        # Una sola scansione dei 273 MiB di stop_times.
        # ----------------------------------------------------

        graph_stops: list[dict] = []
        stop_index: dict[str, int] = {}

        def graph_stop(stop_id: str) -> int | None:
            old = stop_index.get(stop_id)

            if old is not None:
                return old

            stop = all_stops.get(stop_id)

            if not stop:
                return None

            idx = len(graph_stops)
            stop_index[stop_id] = idx
            graph_stops.append(stop)

            return idx

        connections: list[
            tuple[int, int, int, int, int, int, int]
        ] = []

        last_by_trip: dict[
            str,
            tuple[int, int, int | None, int | None]
        ] = {}

        bad_trip_ids: set[str] = set()
        scanned = 0

        for row in gtfs_rows(zf, "stop_times.txt") or []:
            scanned += 1

            if scanned % 1_000_000 == 0:
                print(
                    f"stop_times_scanned={scanned:,} "
                    f"connections={len(connections):,}",
                    file=sys.stderr,
                    flush=True,
                )

            trip_id = str(row.get("trip_id") or "").strip()

            meta = active_trips.get(trip_id)

            if meta is None:
                continue

            stop_id = str(row.get("stop_id") or "").strip()

            stop_idx = graph_stop(stop_id)

            if stop_idx is None:
                continue

            try:
                sequence = int(row.get("stop_sequence") or 0)
            except Exception:
                continue

            arrival_s = gtfs_seconds(row.get("arrival_time"))
            departure_s = gtfs_seconds(row.get("departure_time"))

            previous = last_by_trip.get(trip_id)

            if previous is not None:
                (
                    previous_sequence,
                    previous_stop,
                    previous_arrival,
                    previous_departure,
                ) = previous

                if sequence <= previous_sequence:
                    bad_trip_ids.add(trip_id)
                else:
                    depart = (
                        previous_departure
                        if previous_departure is not None
                        else previous_arrival
                    )

                    arrive = (
                        arrival_s
                        if arrival_s is not None
                        else departure_s
                    )

                    if (
                        depart is not None
                        and arrive is not None
                        and arrive >= depart
                    ):
                        route_id = meta["route_id"]

                        connections.append((
                            previous_stop,
                            stop_idx,
                            route_index[route_id],
                            trip_index[trip_id],
                            depart,
                            arrive,
                            int(meta["direction"]),
                        ))

            last_by_trip[trip_id] = (
                sequence,
                stop_idx,
                arrival_s,
                departure_s,
            )

        print(
            f"stop_times_scanned={scanned:,}",
            file=sys.stderr,
            flush=True,
        )

        print(
            f"connections={len(connections):,}",
            file=sys.stderr,
            flush=True,
        )

        print(
            f"used_stops={len(graph_stops):,}",
            file=sys.stderr,
            flush=True,
        )

        # Il feed GTFS non garantisce necessariamente che tutte le righe
        # di stop_times siano già ordinate per stop_sequence all'interno
        # del trip. La scansione veloce sopra resta O(n); solo i trip
        # effettivamente fuori ordine vengono ricostruiti con una seconda
        # scansione del file.
        if bad_trip_ids:
            print(
                f"repairing_non_monotonic_trips={len(bad_trip_ids)}",
                file=sys.stderr,
                flush=True,
            )

            bad_trip_indices = {
                trip_index[trip_id]
                for trip_id in bad_trip_ids
            }

            # Le connessioni costruite durante la prima scansione per un
            # trip fuori ordine non sono affidabili: le scartiamo tutte.
            connections = [
                item
                for item in connections
                if item[3] not in bad_trip_indices
            ]

            repair_rows: dict[
                str,
                list[
                    tuple[
                        int,
                        int,
                        int | None,
                        int | None,
                    ]
                ],
            ] = {
                trip_id: []
                for trip_id in bad_trip_ids
            }

            # Seconda scansione: memorizziamo soltanto i pochissimi trip
            # risultati non monotoni, non l'intero stop_times.txt.
            for row in gtfs_rows(zf, "stop_times.txt") or []:
                trip_id = str(
                    row.get("trip_id") or ""
                ).strip()

                bucket = repair_rows.get(trip_id)

                if bucket is None:
                    continue

                stop_id = str(
                    row.get("stop_id") or ""
                ).strip()

                stop_idx = graph_stop(stop_id)

                if stop_idx is None:
                    continue

                try:
                    sequence = int(
                        row.get("stop_sequence") or 0
                    )
                except Exception:
                    continue

                bucket.append((
                    sequence,
                    stop_idx,
                    gtfs_seconds(row.get("arrival_time")),
                    gtfs_seconds(row.get("departure_time")),
                ))

            repaired_connections = 0

            for trip_id in sorted(bad_trip_ids):
                rows = repair_rows[trip_id]
                rows.sort(key=lambda item: item[0])

                previous = None
                meta = active_trips[trip_id]

                for current in rows:
                    (
                        sequence,
                        stop_idx,
                        arrival_s,
                        departure_s,
                    ) = current

                    if previous is not None:
                        (
                            previous_sequence,
                            previous_stop,
                            previous_arrival,
                            previous_departure,
                        ) = previous

                        # Dopo il sort un valore <= significa sequenza
                        # realmente duplicata/malata, non semplice
                        # disordine del file. In quel caso fail closed.
                        if sequence <= previous_sequence:
                            raise SystemExit(
                                "stop_sequence duplicata nel trip "
                                f"{trip_id}: {sequence}"
                            )

                        depart = (
                            previous_departure
                            if previous_departure is not None
                            else previous_arrival
                        )

                        arrive = (
                            arrival_s
                            if arrival_s is not None
                            else departure_s
                        )

                        if (
                            depart is not None
                            and arrive is not None
                            and arrive >= depart
                        ):
                            route_id = meta["route_id"]

                            connections.append((
                                previous_stop,
                                stop_idx,
                                route_index[route_id],
                                trip_index[trip_id],
                                depart,
                                arrive,
                                int(meta["direction"]),
                            ))

                            repaired_connections += 1

                    previous = current

            print(
                f"repaired_connections={repaired_connections:,} "
                f"connections_after_repair={len(connections):,}",
                file=sys.stderr,
                flush=True,
            )

    # Connection Scan Algorithm: ordinate per partenza.
    connections.sort(
        key=lambda x: (
            x[4],
            x[5],
            x[2],
            x[3],
            x[0],
            x[1],
        )
    )

    print(
        "building pedestrian transfers...",
        file=sys.stderr,
        flush=True,
    )

    transfers = build_transfers(
        graph_stops,
        args.transfer_radius_m,
        args.max_transfer_neighbors,
    )

    print(
        f"transfers={len(transfers):,}",
        file=sys.stderr,
        flush=True,
    )

    # --------------------------------------------------------
    # Serializzazione.
    # --------------------------------------------------------

    stop_records = bytearray()

    for stop in graph_stops:
        stop_records += STOP.pack(
            int(round(stop["lat"] * 10_000_000)),
            int(round(stop["lon"] * 10_000_000)),
            strings.add(stop["name"]),
            strings.add(stop["id"]),
        )

    route_records = bytearray()

    for item in graph_routes:
        route_records += ROUTE.pack(*item)

    connection_records = bytearray()

    for item in connections:
        connection_records += CONNECTION.pack(*item)

    transfer_records = bytearray()

    for from_stop, to_stop, walk_s in transfers:
        transfer_records += TRANSFER.pack(
            from_stop,
            to_stop,
            walk_s,
            0,
        )

    offset = HEADER.size

    stops_offset = offset
    offset += len(stop_records)

    routes_offset = offset
    offset += len(route_records)

    connections_offset = offset
    offset += len(connection_records)

    transfers_offset = offset
    offset += len(transfer_records)

    strings_offset = offset
    strings_size = len(strings.data)
    offset += strings_size

    service_date_ymd = int(
        service_date.strftime("%Y%m%d")
    )

    header = HEADER.pack(
        MAGIC,
        VERSION,
        service_date_ymd,
        0,
        len(graph_stops),
        len(graph_routes),
        len(connections),
        len(transfers),
        stops_offset,
        routes_offset,
        connections_offset,
        transfers_offset,
        strings_offset,
        strings_size,
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = output_path.with_suffix(
        output_path.suffix + ".tmp"
    )

    with tmp.open("wb") as fp:
        fp.write(header)
        fp.write(stop_records)
        fp.write(route_records)
        fp.write(connection_records)
        fp.write(transfer_records)
        fp.write(strings.data)

        fp.flush()

    tmp.replace(output_path)

    print(f"output={output_path}", file=sys.stderr)
    print(f"bytes={output_path.stat().st_size}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
