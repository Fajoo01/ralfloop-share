#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from ralfloop_agent.integration.gpt_browser_cdp import ChromeCdp
from ralfloop_agent.integration.gpt_queue_shepherd import (
    GptQueueShepherd,
    GptQueueShepherdPolicy,
)
from ralfloop_agent.integration.gpt_work_queue import GptWorkQueue


def main() -> int:
    parser = argparse.ArgumentParser(description="Queue-aware Bot-tazzi GPT tab lifecycle shepherd")
    parser.add_argument("--endpoint", default=os.getenv("BOTTAZZI_GPT_CDP_ENDPOINT", "http://127.0.0.1:9238"))
    parser.add_argument("--db", default=os.getenv("BOTTAZZI_GPT_WORK_QUEUE_DB", "/home/bandi/.local/state/bottazzi/gpt-session/work-queue.sqlite3"))
    parser.add_argument("--complete-idle-ms", type=int, default=int(os.getenv("BOTTAZZI_GPT_COMPLETE_IDLE_MS", "60000")))
    parser.add_argument("--stalled-idle-ms", type=int, default=int(os.getenv("BOTTAZZI_GPT_STALLED_IDLE_MS", "180000")))
    parser.add_argument("--recovery-cooldown-ms", type=int, default=int(os.getenv("BOTTAZZI_GPT_RECOVERY_COOLDOWN_MS", "90000")))
    parser.add_argument("--max-recoveries", type=int, default=int(os.getenv("BOTTAZZI_GPT_MAX_RECOVERIES", "2")))
    parser.add_argument("--no-auto-start", action="store_true")
    args = parser.parse_args()

    queue = GptWorkQueue(args.db)
    cdp = ChromeCdp(args.endpoint)
    policy = GptQueueShepherdPolicy(
        complete_idle_ms=args.complete_idle_ms,
        stalled_idle_ms=args.stalled_idle_ms,
        recovery_cooldown_ms=args.recovery_cooldown_ms,
        max_recoveries=args.max_recoveries,
    )
    report = GptQueueShepherd(queue, cdp, policy=policy).run_once(
        auto_start=not args.no_auto_start
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
