#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path


DEFAULT_GTFS = Path(
    "/home/sibilla-cumana/ralfloop_data/atm_telegram/gtfs.zip"
)
DEFAULT_GRAPH = Path(
    "/home/sibilla-cumana/ralfloop_data/atm_telegram/atm-router-current.bin"
)


def run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gtfs", type=Path, default=DEFAULT_GTFS)
    parser.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    builder = root / "tools/atm_router/build_graph.py"
    router = root / "tools/atm_router/atm-router"

    if not args.gtfs.is_file():
        raise SystemExit(f"GTFS non trovato: {args.gtfs}")

    if not builder.is_file():
        raise SystemExit(f"builder non trovato: {builder}")

    if not router.is_file():
        raise SystemExit(f"router non trovato: {router}")

    args.graph.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=".atm-router-",
        suffix=".bin",
        dir=str(args.graph.parent),
    )
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.unlink(missing_ok=True)

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

        os.chmod(tmp, 0o664)
        os.replace(tmp, args.graph)

        print(
            "ATM_GRAPH_REFRESH_OK "
            f"date={args.date} "
            f"bytes={args.graph.stat().st_size}"
        )

        return 0

    finally:
        tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
