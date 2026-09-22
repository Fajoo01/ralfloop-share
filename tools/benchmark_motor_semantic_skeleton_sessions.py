#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import tempfile
from typing import Any

from ralfloop_agent.integration.motor_semantic_skeleton import (
    ContextSegment,
    compact_context,
)


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]


def _history(record: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in record.get("history") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        rows.append({"role": str(item.get("role") or "unknown"), "content": content})
    return rows

def _segments(rows: list[dict[str, str]]) -> list[ContextSegment]:
    output: list[ContextSegment] = []
    code = {"user": "U", "assistant": "A", "system": "S", "tool": "T"}
    for index, row in enumerate(rows):
        role = row["role"].casefold()
        output.append(ContextSegment(
            text=f"{code.get(role, '?')}>{row['content']}",
            ref=f"turn:{index}",
            kind="history",
            certainty="?",
            priority=10,
            exact=False,
        ))
    return output


def _raw_text(rows: list[dict[str, str]]) -> str:
    code = {"user": "U", "assistant": "A", "system": "S", "tool": "T"}
    return "\n".join(
        f"{code.get(row['role'].casefold(), '?')}>{row['content']}" for row in rows
    )


def _ds4_count(binary: Path, model: Path, text: str) -> int:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        handle.write(text)
        path = Path(handle.name)
    try:
        completed = subprocess.run(
            [str(binary), "--raw", "--dump-tokens", "-m", str(model), "--prompt-file", str(path)],
            check=True, capture_output=True, text=True, timeout=60,
        )
        first = completed.stdout.splitlines()[0]
        ids = ast.literal_eval(first)
        if not isinstance(ids, list):
            raise ValueError("unexpected_token_dump")
        return len(ids)
    finally:
        path.unlink(missing_ok=True)

def _load_candidates(root: Path, min_chars: int) -> list[tuple[Path, dict[str, Any], list[dict[str, str]]]]:
    output = []
    for path in root.glob("*.json"):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        rows = _history(record)
        if sum(len(row["content"]) for row in rows) < min_chars:
            continue
        output.append((path, record, rows))
    output.sort(key=lambda item: sum(len(row["content"]) for row in item[2]), reverse=True)
    return output


def _aggregate(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    p90_index = min(len(ordered) - 1, max(0, int(round(0.9 * (len(ordered) - 1)))))
    return {
        "min": round(min(values), 4),
        "median": round(statistics.median(values), 4),
        "mean": round(statistics.fmean(values), 4),
        "p90": round(ordered[p90_index], 4),
        "max": round(max(values), 4),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.home() / ".local/state/ralf/sessions")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--min-chars", type=int, default=200)
    parser.add_argument("--profile", choices=("safe", "dense"), default="safe")
    parser.add_argument("--ds4-bin", type=Path)
    parser.add_argument("--model", type=Path)
    args = parser.parse_args()

    candidates = _load_candidates(args.root, args.min_chars)[: max(0, args.limit)]
    results: list[dict[str, Any]] = []
    for _, record, rows in candidates:
        raw = _raw_text(rows)
        skeleton = compact_context(_segments(rows), use_grammar=True, profile=args.profile)
        compact = "\n".join(entry.text for entry in skeleton.entries)
        row: dict[str, Any] = {
            "session": _session_hash(str(record.get("session_id") or "")),
            "turns": len(rows),
            "raw_chars": len(raw),
            "compact_chars": len(compact),
            "char_ratio": round(len(compact) / len(raw), 4) if raw else 1.0,
            "grammar_tokens": skeleton.grammar_tokens,
            "grammar_hits": skeleton.grammar_hits,
            "guard_fallbacks": skeleton.guard_fallbacks,
        }
        if args.ds4_bin and args.model:
            raw_tokens = _ds4_count(args.ds4_bin, args.model, raw)
            compact_tokens = _ds4_count(args.ds4_bin, args.model, compact)
            row.update({
                "raw_tokens": raw_tokens,
                "compact_tokens": compact_tokens,
                "token_ratio": round(compact_tokens / raw_tokens, 4) if raw_tokens else 1.0,
            })
        results.append(row)

    report: dict[str, Any] = {
        "sessions": len(results),
        "profile": args.profile,
        "content_published": False,
        "rows": results,
        "char_ratio": _aggregate([float(row["char_ratio"]) for row in results]),
    }
    token_ratios = [float(row["token_ratio"]) for row in results if "token_ratio" in row]
    if token_ratios:
        report["token_ratio"] = _aggregate(token_ratios)
        raw_total = sum(int(row["raw_tokens"]) for row in results)
        compact_total = sum(int(row["compact_tokens"]) for row in results)
        report["token_total"] = {
            "raw": raw_total,
            "compact": compact_total,
            "ratio": round(compact_total / raw_total, 4) if raw_total else 1.0,
        }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
