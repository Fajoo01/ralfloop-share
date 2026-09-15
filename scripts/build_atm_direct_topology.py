#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any


def rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig")
        yield from csv.DictReader(text)


def gtfs_seconds(value: str | None) -> int | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        hour, minute, second = (int(x) for x in value.split(":"))
    except (TypeError, ValueError):
        return None
    seconds = hour * 3600 + minute * 60 + second
    return seconds if 0 <= seconds <= 172800 else None


def line_label(row: dict[str, str]) -> str:
    short = str(row.get("route_short_name") or "").strip()
    route_id = str(row.get("route_id") or "").strip()
    route_type = str(row.get("route_type") or "").strip()
    if route_type == "1":
        upper = short.upper()
        if upper.startswith("M"):
            return upper
        if short:
            return f"M{short}"
        if route_id.upper().startswith("M"):
            return route_id.upper()
    return short or route_id


def build(gtfs: Path) -> dict[str, Any]:
    with zipfile.ZipFile(gtfs) as zf:
        route_meta = {
            row["route_id"]: {
                "line": line_label(row),
                "route_type": str(row.get("route_type") or ""),
            }
            for row in rows(zf, "routes.txt")
            if row.get("route_id")
        }

        stops: dict[str, dict[str, Any]] = {}
        for row in rows(zf, "stops.txt"):
            stop_id = str(row.get("stop_id") or "").strip()
            if not stop_id:
                continue
            try:
                lat = float(row.get("stop_lat") or "")
                lon = float(row.get("stop_lon") or "")
            except ValueError:
                continue
            stops[stop_id] = {
                "id": stop_id,
                "name": str(row.get("stop_name") or stop_id).strip(),
                "lat": lat,
                "lon": lon,
            }

        representatives: dict[tuple[str, str, str], str] = {}
        trip_meta: dict[str, dict[str, str]] = {}
        for row in rows(zf, "trips.txt"):
            route_id = str(row.get("route_id") or "").strip()
            trip_id = str(row.get("trip_id") or "").strip()
            if route_id not in route_meta or not trip_id:
                continue
            direction = str(row.get("direction_id") or "").strip()
            variant = str(row.get("shape_id") or row.get("trip_headsign") or trip_id).strip()
            key = (route_id, direction, variant)
            if key in representatives:
                continue
            representatives[key] = trip_id
            trip_meta[trip_id] = {
                **route_meta[route_id],
                "direction": direction,
                "variant": variant,
            }

        wanted = set(trip_meta)
        stop_sequences: dict[str, list[dict[str, Any]]] = {trip_id: [] for trip_id in wanted}
        for row in rows(zf, "stop_times.txt"):
            trip_id = str(row.get("trip_id") or "").strip()
            if trip_id not in wanted:
                continue
            stop_id = str(row.get("stop_id") or "").strip()
            if stop_id not in stops:
                continue
            try:
                sequence = int(row.get("stop_sequence") or 0)
            except ValueError:
                sequence = 0
            stop_sequences[trip_id].append({
                "sequence": sequence,
                "stop_id": stop_id,
                "arrival_s": gtfs_seconds(row.get("arrival_time")),
                "departure_s": gtfs_seconds(row.get("departure_time")),
            })

        patterns: list[dict[str, Any]] = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        for trip_id, meta in trip_meta.items():
            ordered = sorted(stop_sequences.get(trip_id) or [], key=lambda item: item["sequence"])
            ordered_ids = tuple(item["stop_id"] for item in ordered)
            if len(ordered_ids) < 2:
                continue
            key = (meta["line"], meta["direction"], ordered_ids)
            if key in seen:
                continue
            seen.add(key)
            pattern_stops = []
            for item in ordered:
                pattern_stops.append({
                    **stops[item["stop_id"]],
                    "arrival_s": item["arrival_s"],
                    "departure_s": item["departure_s"],
                })
            patterns.append({
                "line": meta["line"],
                "direction": meta["direction"],
                "route_type": meta["route_type"],
                "stops": pattern_stops,
            })

        feed_info = {}
        try:
            feed_info = next(rows(zf, "feed_info.txt"), {})
        except KeyError:
            pass

    return {
        "version": 2,
        "source": str(gtfs),
        "feed_version": str(feed_info.get("feed_version") or ""),
        "surface_end_date": str(feed_info.get("surface_end_date") or ""),
        "patterns": patterns,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()
    payload = build(args.gtfs)
    if not payload["patterns"]:
        raise SystemExit("nessun pattern GTFS prodotto")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".atm-topology-", suffix=".json", dir=args.output.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.chmod(tmp, 0o664)
        os.replace(tmp, args.output)
    finally:
        tmp.unlink(missing_ok=True)
    print(f"ATM_DIRECT_TOPOLOGY_OK patterns={len(payload['patterns'])} bytes={args.output.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
