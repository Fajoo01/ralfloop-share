#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from ralfloop_agent.unified_assistant.call_agenda_trigger import (
    CallAgendaHttpIntake,
    CallAgendaTrigger,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bot-tazzi ready-call-transcript to Agenda bridge")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=10.0)
    return parser


def _build_trigger() -> CallAgendaTrigger:
    recordings = Path(os.getenv(
        "BOTTAZZI_CALL_RECORDINGS_DIR",
        "/var/lib/ralfloop-bottazzi-call-recordings",
    ))
    state_path = Path(os.getenv(
        "BOTTAZZI_CALL_AGENDA_STATE",
        "/var/lib/ralfloop-bottazzi-call-agenda/state.json",
    ))
    return CallAgendaTrigger(
        recordings_root=recordings,
        agenda_intake=CallAgendaHttpIntake.from_env(),
        state_path=state_path,
    )


def _run_once(trigger: CallAgendaTrigger) -> int:
    result = trigger.poll()
    if result.ready_seen or result.missing_transcript:
        print(json.dumps(result.__dict__, sort_keys=True), flush=True)
    return result.ingested


def main() -> int:
    args = _parser().parse_args()
    trigger = _build_trigger()
    if args.once:
        _run_once(trigger)
        return 0
    interval = max(2.0, args.interval)
    while True:
        try:
            processed = _run_once(trigger)
            if processed == 0:
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"call_agenda_bridge_error={exc.__class__.__name__}", file=sys.stderr, flush=True)
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
