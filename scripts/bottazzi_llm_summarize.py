#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def median(rows: list[dict], key: str) -> float | None:
    values = [float(row[key]) for row in rows if row.get(key) is not None]
    return round(statistics.median(values), 3) if values else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.files:
        rows = []
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("ok"):
                rows.append(row)
        summary = {
            "file": str(path),
            "samples": len(rows),
            "success_rate": round(
                sum(bool((row.get("score") or {}).get("success")) for row in rows)
                / len(rows), 3,
            ) if rows else None,
            "median_ttft_seconds": median(rows, "ttft_seconds"),
            "median_wall_seconds": median(rows, "wall_seconds"),
            "median_prefill_tps": median(rows, "prefill_tps"),
            "median_decode_tps": median(rows, "decode_tps"),
        }
        print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
