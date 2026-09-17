#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import date
from pathlib import Path


DEFAULT_GTFS = Path(
    "/home/sibilla-cumana/ralfloop_data/atm_telegram/gtfs.zip"
)
DEFAULT_GTFS_URL = os.environ.get(
    "RALFLOOP_ATM_GTFS_URL",
    "https://dati.comune.milano.it/gtfs.zip",
)
DEFAULT_GRAPH = Path(
    "/home/sibilla-cumana/ralfloop_data/atm_telegram/atm-router-current.bin"
)
DEFAULT_TOPOLOGY = Path(
    "/home/sibilla-cumana/ralfloop_data/atm_telegram/atm-direct-topology.json"
)


def run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def gtfs_metadata(path: Path) -> dict[str, str]:
    required = {"routes.txt", "stops.txt", "trips.txt", "stop_times.txt"}
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        missing = sorted(required - names)
        if missing:
            raise ValueError("GTFS incompleto: " + ", ".join(missing))
        try:
            with zf.open("feed_info.txt") as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig")
                row = next(csv.DictReader(text), {})
        except KeyError:
            row = {}
    return {str(k): str(v or "") for k, v in row.items()}


def surface_schedule_fresh(metadata: dict[str, str], target_date: str) -> bool:
    end_date = str(metadata.get("surface_end_date") or "").strip()
    expected = target_date.replace("-", "")
    return len(end_date) == 8 and end_date.isdigit() and end_date >= expected


def refresh_gtfs(url: str, destination: Path) -> dict[str, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".gtfs-", suffix=".zip", dir=destination.parent)
    os.close(fd)
    tmp = Path(name)
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "ralfloop-atm-gtfs-refresh/1.0"}
        )
        with urllib.request.urlopen(request, timeout=60) as response, tmp.open("wb") as out:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        metadata = gtfs_metadata(tmp)
        os.chmod(tmp, 0o664)
        os.replace(tmp, destination)
        return metadata
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs", type=Path, default=DEFAULT_GTFS)
    parser.add_argument("--gtfs-url", default=DEFAULT_GTFS_URL)
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--topology", type=Path, default=DEFAULT_TOPOLOGY)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    builder = root / "tools/atm_router/build_graph.py"
    topology_builder = root / "scripts/build_atm_direct_topology.py"
    router = root / "tools/atm_router/atm-router"

    download_error = ""
    if not args.skip_download and args.gtfs_url:
        try:
            metadata = refresh_gtfs(str(args.gtfs_url), args.gtfs)
            print(
                "ATM_GTFS_DOWNLOAD_OK "
                f"feed_version={metadata.get('feed_version') or '?'} "
                f"surface_end_date={metadata.get('surface_end_date') or '?'}"
            )
        except Exception as exc:
            download_error = str(exc)
            print(f"ATM_GTFS_DOWNLOAD_WARN {download_error}", file=sys.stderr)

    if not args.gtfs.is_file():
        raise SystemExit(f"GTFS non trovato: {args.gtfs}")

    try:
        feed_metadata = gtfs_metadata(args.gtfs)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SystemExit(f"GTFS non valido: {exc}")
    surface_fresh = surface_schedule_fresh(feed_metadata, args.date)

    if not builder.is_file():
        raise SystemExit(f"builder non trovato: {builder}")

    if not router.is_file():
        raise SystemExit(f"router non trovato: {router}")

    if not topology_builder.is_file():
        raise SystemExit(f"topology builder non trovato: {topology_builder}")

    args.graph.parent.mkdir(parents=True, exist_ok=True)
    args.topology.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=".atm-router-",
        suffix=".bin",
        dir=str(args.graph.parent),
    )
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.unlink(missing_ok=True)

    topo_fd, topo_name = tempfile.mkstemp(
        prefix=".atm-topology-",
        suffix=".json",
        dir=str(args.topology.parent),
    )
    os.close(topo_fd)
    tmp_topology = Path(topo_name)
    tmp_topology.unlink(missing_ok=True)

    try:
        built = run(
            [
                sys.executable,
                str(builder),
                "--gtfs",
                str(args.gtfs),
                "--date",
                args.date,
                "--output",
                str(tmp),
            ],
            timeout=300,
        )

        sys.stdout.write(built.stdout)
        sys.stderr.write(built.stderr)

        if built.returncode != 0:
            raise SystemExit(
                f"build fallita rc={built.returncode}"
            )

        if not tmp.is_file() or tmp.stat().st_size < 1024:
            raise SystemExit("grafo prodotto non valido")

        # Validazione reale col binario C prima dello swap.
        # Coordinate centrali di Milano usate solo come probe del grafo.
        checked = run(
            [
                str(router),
                "--nearby",
                str(tmp),
                "45.4642",
                "9.1900",
                "1200",
                "5",
            ],
            timeout=10,
        )

        if checked.returncode != 0:
            sys.stderr.write(checked.stderr)
            raise SystemExit(
                f"validazione router fallita rc={checked.returncode}"
            )

        try:
            payload = json.loads(checked.stdout)
        except ValueError as exc:
            raise SystemExit(
                f"output router non JSON: {exc}"
            )

        expected = int(args.date.replace("-", ""))

        if payload.get("status") != "ok":
            raise SystemExit(
                f"status grafo inatteso: {payload.get('status')}"
            )

        if int(payload.get("service_date") or 0) != expected:
            raise SystemExit(
                "service_date errata: "
                f"{payload.get('service_date')} != {expected}"
            )

        if not payload.get("stops"):
            raise SystemExit(
                "validazione fallita: nessuna fermata"
            )

        topology = run(
            [
                sys.executable,
                str(topology_builder),
                "--gtfs",
                str(args.gtfs),
                "--output",
                str(tmp_topology),
            ],
            timeout=300,
        )
        sys.stdout.write(topology.stdout)
        sys.stderr.write(topology.stderr)
        if topology.returncode != 0:
            raise SystemExit(
                f"build topologia fallita rc={topology.returncode}"
            )
        try:
            topology_payload = json.loads(
                tmp_topology.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise SystemExit(f"topologia non valida: {exc}")
        if len(topology_payload.get("patterns") or []) < 10:
            raise SystemExit("topologia non valida: pattern insufficienti")

        os.chmod(tmp, 0o664)
        os.chmod(tmp_topology, 0o664)
        os.replace(tmp, args.graph)
        os.replace(tmp_topology, args.topology)

        status = "ATM_GRAPH_REFRESH_OK" if surface_fresh else "ATM_GRAPH_REFRESH_PARTIAL"
        print(
            status + " "
            f"date={args.date} "
            f"bytes={args.graph.stat().st_size} "
            f"topology_patterns={len(topology_payload.get('patterns') or [])} "
            f"feed_version={feed_metadata.get('feed_version') or '?'} "
            f"surface_end_date={feed_metadata.get('surface_end_date') or '?'} "
            f"surface_fresh={1 if surface_fresh else 0} "
            f"download_fallback={1 if download_error else 0}"
        )

        return 0

    finally:
        tmp.unlink(missing_ok=True)
        tmp_topology.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
