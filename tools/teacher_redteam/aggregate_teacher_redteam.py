#!/usr/bin/env python3
from __future__ import annotations
import argparse
import json
from pathlib import Path
import importlib.util

ROOT = Path(__file__).resolve().parents[2]
HARNESS = ROOT / "tools/teacher_redteam/run_teacher_redteam.py"

def load_harness():
    spec = importlib.util.spec_from_file_location("teacher_redteam_harness", HARNESS)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module

def load(path: Path):
    rows = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    meta = rows[0].get("_metadata", {})
    return meta, rows[1:]

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("parts", nargs="+", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args()
    metas, cases = [], []
    for part in args.parts:
        meta, rows = load(part)
        metas.append(meta)
        cases.extend(rows)
    cases.sort(key=lambda row: row["id"])
    seen = set()
    unique = []
    for row in cases:
        if row["id"] in seen:
            raise SystemExit(f"duplicate_case:{row['id']}")
        seen.add(row["id"])
        unique.append(row)
    metadata = {
        "model": "+".join(sorted({m.get("model","unknown") for m in metas})),
        "tool_count": min((m.get("tool_count",0) for m in metas), default=0),
        "production_current": metas[0].get("production_current","") if metas else "",
        "parts": [str(p) for p in args.parts],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w") as fh:
        fh.write(json.dumps({"_metadata": metadata}, ensure_ascii=False) + "\n")
        for row in unique:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    harness = load_harness()
    args.report.write_text(harness.render_report(unique, metadata))
    print(json.dumps({
        "cases": len(unique),
        "out": str(args.out),
        "report": str(args.report)
    }))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
