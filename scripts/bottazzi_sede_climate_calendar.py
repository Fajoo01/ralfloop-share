#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.bottazzi_climate.runtime import load_policy, run_calendar_climate_cycle


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Calendar -> Sede climate planner cycle")
    parser.add_argument(
        "--policy",
        default=str(ROOT / "config" / "sede_climate_policy.json"),
    )
    parser.add_argument("--now", help="ISO timestamp override for diagnostics/tests")
    parser.add_argument("--no-persist", action="store_true")
    args = parser.parse_args()
    policy = load_policy(args.policy)
    now = datetime.fromisoformat(args.now) if args.now else None
    result = run_calendar_climate_cycle(
        policy,
        now=now,
        persist=not args.no_persist,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
