#!/usr/bin/env python3
from __future__ import annotations

"""Background Bot-tazzi watcher for MD/Goodify outcome email."""

import argparse
import json
import os
from pathlib import Path
import sys
import time

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.md_goodify import DEFAULT_FORWARD_TO, process_goodify_mailbox


def run_once() -> dict:
    account = os.getenv("RALFLOOP_MD_GOODIFY_ACCOUNT", "").strip()
    if not account:
        raise RuntimeError("RALFLOOP_MD_GOODIFY_ACCOUNT_required")
    return process_goodify_mailbox(
        account=account,
        forward_to=os.getenv("RALFLOOP_MD_GOODIFY_FORWARD_TO", DEFAULT_FORWARD_TO).strip(),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=float(os.getenv("RALFLOOP_MD_GOODIFY_POLL_SEC", "60")))
    args = parser.parse_args()

    if args.once:
        print(json.dumps(run_once(), ensure_ascii=False, sort_keys=True))
        return 0
    interval = max(30.0, args.interval)
    while True:
        try:
            result = run_once()
            if result.get("processed_now"):
                print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)[:240]}, ensure_ascii=False), file=sys.stderr, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
