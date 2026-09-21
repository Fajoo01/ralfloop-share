#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
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
DEFAULT_GRAPH = Path(
    "/home/sibilla-cumana/ralfloop_data/atm_telegram/atm-router-current.bin"
)
DEFAULT_TOPOLOGY = Path(
    "/home/sibilla-cumana/ralfloop_data/atm_telegram/atm-direct-topology.json"
)
DEFAULT_GTFS_URL = os.getenv(
    "RALFLOOP_ATM_GTFS_URL",
    "https://dati.comune.milano.it/gtfs.zip",
).strip()
REQUIRED_GTFS_FILES = {
    "feed_info.txt",
    "routes.txt",
    "trips.txt",
    "stops.txt",
    "stop_times.txt",
}


def run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _service_day(value: str) -> int:
    compact = str(value or "").strip().replace("-", "")
    if len(compact) != 8 or not compact.isdigit():
        raise ValueError(f"data servizio non valida: {value!r}")
    return int(compact)


def _read_feed_info(gtfs: Path) -> dict[str, str]:
    try:
        with zipfile.ZipFile(gtfs) as zf:
            names = set(zf.namelist())
            missing = sorted(REQUIRED_GTFS_FILES - names)
            if missing:
                raise ValueError(
                    "GTFS incompleto, file mancanti: " + ", ".join(missing)
                )
            bad_member = zf.testzip()
            if bad_member:
                raise ValueError(f"GTFS corrotto: {bad_member}")
            with zf.open("feed_info.txt") as raw:
                reader = csv.DictReader(
                    io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
                )
                row = next(reader, None)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ValueError(f"GTFS non leggibile: {exc}") from exc
    if not row:
        raise ValueError("feed_info.txt vuoto")
    return {str(key): str(value or "").strip() for key, value in row.items()}


def _validate_feed_window(gtfs: Path, service_date: str) -> dict[str, str]:
    info = _read_feed_info(gtfs)
    wanted = _service_day(service_date)
    start_raw = info.get("feed_start_date") or info.get("surface_start_date") or ""
    end_raw = info.get("surface_end_date") or info.get("feed_end_date") or ""
    if not end_raw:
        raise ValueError("feed_info senza surface_end_date/feed_end_date")
    end = _service_day(end_raw)
    if end < wanted:
        raise ValueError(
            f"GTFS superficie scaduto: {end_raw} < {service_date}"
        )
    if start_raw:
        start = _service_day(start_raw)
        if start > wanted:
            raise ValueError(
                f"GTFS non ancora valido: {start_raw} > {service_date}"
            )
    return info


def _download_gtfs(url: str, destination: Path, *, timeout: int = 90) -> None:
    if not url:
        raise ValueError("URL GTFS ufficiale non configurato")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/zip, application/octet-stream;q=0.9, */*;q=0.1",
            "User-Agent": "ralfloop-atm-gtfs-refresh/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = int(getattr(response, "status", 200) or 200)
        if status >= 400:
            raise OSError(f"download GTFS HTTP {status}")
        with destination.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
    if destination.stat().st_size < 1024:
        raise ValueError("download GTFS troppo piccolo")


def _download_candidate(gtfs: Path, url: str, service_date: str) -> tuple[Path, dict[str, str]]:
    gtfs.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".gtfs-download-",
        suffix=".zip",
        dir=str(gtfs.parent),
    )
    os.close(fd)
    candidate = Path(name)
    try:
        _download_gtfs(url, candidate)
        info = _validate_feed_window(candidate, service_date)
        return candidate, info
    except Exception:
        candidate.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs", type=Path, default=DEFAULT_GTFS)
    parser.add_argument("--gtfs-url", default=DEFAULT_GTFS_URL)
    parser.add_argument(
        "--no-download",
        action="store_true",
        help="usa il GTFS locale senza scaricarlo, ma ne valida comunque la finestra",
    )
    parser.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--topology", type=Path, default=DEFAULT_TOPOLOGY)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    builder = root / "tools/atm_router/build_graph.py"
    topology_builder = root / "scripts/build_atm_direct_topology.py"
    router = root / "tools/atm_router/atm-router"

    if not builder.is_file():
        raise SystemExit(f"builder non trovato: {builder}")
    if not router.is_file():
        raise SystemExit(f"router non trovato: {router}")
    if not topology_builder.is_file():
        raise SystemExit(f"topology builder non trovato: {topology_builder}")

    args.graph.parent.mkdir(parents=True, exist_ok=True)
    args.topology.parent.mkdir(parents=True, exist_ok=True)

    downloaded_gtfs: Path | None = None
    source_gtfs = args.gtfs
    feed_info: dict[str, str]

    if args.no_download:
        if not args.gtfs.is_file():
            raise SystemExit(f"GTFS non trovato: {args.gtfs}")
        try:
            feed_info = _validate_feed_window(args.gtfs, args.date)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
    else:
        try:
            downloaded_gtfs, feed_info = _download_candidate(
                args.gtfs,
                str(args.gtfs_url or "").strip(),
                args.date,
            )
            source_gtfs = downloaded_gtfs
            print(
                "ATM_GTFS_DOWNLOAD_OK "
                f"feed_version={feed_info.get('feed_version') or 'n/d'} "
                f"surface_end_date={feed_info.get('surface_end_date') or 'n/d'} "
                f"bytes={source_gtfs.stat().st_size}"
            )
        except Exception as exc:
            print(
                f"ATM_GTFS_DOWNLOAD_WARNING {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            if not args.gtfs.is_file():
                raise SystemExit("download GTFS fallito e nessun feed locale disponibile") from exc
            try:
                feed_info = _validate_feed_window(args.gtfs, args.date)
            except ValueError as local_exc:
                raise SystemExit(
                    "download GTFS fallito e feed locale non utilizzabile: "
                    f"{local_exc}"
                ) from exc
            source_gtfs = args.gtfs
            print(
                "ATM_GTFS_LOCAL_FALLBACK_OK "
                f"feed_version={feed_info.get('feed_version') or 'n/d'} "
                f"surface_end_date={feed_info.get('surface_end_date') or 'n/d'}"
            )

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
                str(source_gtfs),
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
            raise SystemExit(f"build fallita rc={built.returncode}")
        if not tmp.is_file() or tmp.stat().st_size < 1024:
            raise SystemExit("grafo prodotto non valido")

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
            raise SystemExit(f"validazione router fallita rc={checked.returncode}")
        try:
            payload = json.loads(checked.stdout)
        except ValueError as exc:
            raise SystemExit(f"output router non JSON: {exc}") from exc

        expected = _service_day(args.date)
        if payload.get("status") != "ok":
            raise SystemExit(f"status grafo inatteso: {payload.get('status')}")
        if int(payload.get("service_date") or 0) != expected:
            raise SystemExit(
                "service_date errata: "
                f"{payload.get('service_date')} != {expected}"
            )
        if not payload.get("stops"):
            raise SystemExit("validazione fallita: nessuna fermata")

        topology = run(
            [
                sys.executable,
                str(topology_builder),
                "--gtfs",
                str(source_gtfs),
                "--output",
                str(tmp_topology),
            ],
            timeout=300,
        )
        sys.stdout.write(topology.stdout)
        sys.stderr.write(topology.stderr)
        if topology.returncode != 0:
            raise SystemExit(f"build topologia fallita rc={topology.returncode}")
        try:
            topology_payload = json.loads(tmp_topology.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"topologia non valida: {exc}") from exc
        if len(topology_payload.get("patterns") or []) < 10:
            raise SystemExit("topologia non valida: pattern insufficienti")
        if str(topology_payload.get("feed_version") or "") != str(feed_info.get("feed_version") or ""):
            raise SystemExit("topologia non valida: feed_version incoerente")
        try:
            _service_day(str(topology_payload.get("surface_end_date") or ""))
        except ValueError as exc:
            raise SystemExit(f"topologia non valida: {exc}") from exc

        os.chmod(tmp, 0o664)
        os.chmod(tmp_topology, 0o664)
        os.replace(tmp, args.graph)
        os.replace(tmp_topology, args.topology)
        if downloaded_gtfs is not None:
            os.chmod(downloaded_gtfs, 0o664)
            os.replace(downloaded_gtfs, args.gtfs)
            downloaded_gtfs = None

        print(
            "ATM_GRAPH_REFRESH_OK "
            f"date={args.date} "
            f"feed_version={feed_info.get('feed_version') or 'n/d'} "
            f"surface_end_date={feed_info.get('surface_end_date') or 'n/d'} "
            f"bytes={args.graph.stat().st_size} "
            f"topology_patterns={len(topology_payload.get('patterns') or [])}"
        )
        return 0
    finally:
        tmp.unlink(missing_ok=True)
        tmp_topology.unlink(missing_ok=True)
        if downloaded_gtfs is not None:
            downloaded_gtfs.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
