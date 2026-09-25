#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import statistics


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Browser Rizzo shadow events")
    parser.add_argument(
        "path",
        nargs="?",
        default="/home/sibilla-cumana/.local/state/ralfloop/browser-rizzo-shadow.jsonl",
    )
    args = parser.parse_args()
    path = Path(args.path)
    if not path.exists():
        print(json.dumps({"events": 0, "reason": "audit_missing"}))
        return 0

    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)

    ok = [row for row in rows if row.get("status") == "ok"]
    comparable = [row for row in ok if row.get("proposal_target")]
    matches = [row for row in comparable if row.get("match") is True]
    misses = [row for row in comparable if row.get("match") is False]
    shortlist_misses = [row for row in ok if row.get("authoritative_in_shortlist") is False]
    model_misses = [
        row for row in misses
        if row.get("authoritative_in_shortlist") is True
    ]
    latencies = [float(row["latency_ms"]) for row in ok if isinstance(row.get("latency_ms"), (int, float))]
    result = {
        "events": len(rows),
        "ok": len(ok),
        "comparable": len(comparable),
        "matches": len(matches),
        "accuracy": (len(matches) / len(comparable)) if comparable else None,
        "shortlist_misses": len(shortlist_misses),
        "model_misses": len(model_misses),
        "errors": sum(row.get("status") == "shadow_error" for row in rows),
        "dropped_queue_full": sum(row.get("status") == "dropped_queue_full" for row in rows),
        "latency_ms": {
            "median": statistics.median(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "sources": {
            source: sum(row.get("proposal_source") == source for row in ok)
            for source in ("deterministic", "rizzo", "fallback", "none")
        },
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
