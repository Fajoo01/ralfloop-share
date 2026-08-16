#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ralfloop_agent.coding_harness import HarnessConfig, run_harness


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pi + AgentCPM coding harness with deterministic validation and DS4 final judge."
    )
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--validate", required=True)
    parser.add_argument("--worker-timeout", type=int, default=120)
    parser.add_argument("--validator-timeout", type=int, default=180)
    parser.add_argument("--allow-test-changes", action="store_true")
    parser.add_argument("--protect", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    config = HarnessConfig(
        workdir=args.workdir,
        task=args.task,
        validator_command=args.validate,
        worker_timeout_sec=args.worker_timeout,
        validator_timeout_sec=args.validator_timeout,
        allow_test_changes=args.allow_test_changes,
        protected_globs=tuple(args.protect),
    )

    report = run_harness(config)

    raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(raw, encoding="utf-8")

    print(raw, end="")
    return 0 if report.get("final_status") == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
