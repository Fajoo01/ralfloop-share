#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics


def mean(rows: list[dict], key: str):
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return statistics.fmean(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--json", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args()
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in sorted(args.directory.glob("*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                grouped[(row["scenario"], row["ngram_pool_scope"])].append(row)
    summaries = []
    log_pattern = re.compile(r"dur\(b,g,a\)\s*=\s*([0-9.]+),\s*([0-9.]+),\s*([0-9.]+)\s*ms")
    for (scenario, scope), rows in sorted(grouped.items()):
        log_path = args.directory / f"{scenario}-{scope}.server.log"
        cpu_ms = None
        if log_path.exists():
            matches = log_pattern.findall(log_path.read_text(errors="replace"))
            if matches:
                cpu_ms = sum(float(value) for value in matches[-1])
        proposed = sum(int(row.get("speculative_proposed_tokens") or 0) for row in rows)
        accepted = sum(int(row.get("speculative_accepted_tokens") or 0) for row in rows)
        summaries.append({
            "scenario": scenario,
            "scope": scope,
            "runs": len(rows),
            "acceptance": accepted / proposed if proposed else None,
            "decode_tps": mean(rows, "decode_tps"),
            "ttft_seconds": mean(rows, "ttft_seconds"),
            "wall_seconds": mean(rows, "wall_seconds"),
            "ngram_lookup_cpu_seconds_estimate": mean(rows, "ngram_lookup_cpu_seconds_estimate"),
            "server_ngram_cpu_ms_cumulative": cpu_ms,
            "correctness": sum(bool(row.get("correct")) for row in rows) / len(rows),
            "cached_prompt_token_ratio": mean(rows, "prompt_cached_token_ratio"),
            "pool_bytes": max((int(row.get("ngram_pool_bytes") or 0) for row in rows), default=0),
        })
    args.json.write_text(json.dumps(summaries, indent=2, sort_keys=True) + "\n")
    lines = [
        "| Scenario | Scope | Runs | Acceptance | Decode tok/s | TTFT s | Wall s | Correct |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summaries:
        acceptance = "—" if item["acceptance"] is None else f'{100 * item["acceptance"]:.1f}%'
        lines.append(
            f'| {item["scenario"]} | {item["scope"]} | {item["runs"]} | {acceptance} | '
            f'{item["decode_tps"]:.2f} | {item["ttft_seconds"]:.2f} | '
            f'{item["wall_seconds"]:.2f} | {100 * item["correctness"]:.0f}% |'
        )
    args.markdown.write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
