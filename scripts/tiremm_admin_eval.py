#!/usr/bin/env python3
from __future__ import annotations

import json
import argparse
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from test_tiremm_admin_vertical import EVAL_CASES  # noqa: E402
from tiremm_admin_fixture import build_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rows = []
    for case_id, check in EVAL_CASES:
        passed = bool(check(build_store()))
        rows.append({"case_id": case_id, "passed": passed})
    passed = sum(row["passed"] for row in rows)
    payload = json.dumps({
        "schema_version": 1,
        "suite": "tiremm_admin_eval_v0",
        "passed": passed,
        "total": len(rows),
        "pass_rate": passed / len(rows),
        "cases": rows,
    }, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
