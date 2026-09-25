#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from ralfloop_agent.unified_assistant.agenda_ingress import build_default_agenda_intake
from ralfloop_agent.unified_assistant.call_agenda_trigger import CallAgendaTrigger
from ralfloop_agent.unified_assistant.memory_service import MemoryService


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bot-tazzi ready-call-transcript direct Agenda bridge")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=float, default=10.0)
    return parser


def _paths() -> tuple[Path, Path, Path]:
    recordings = Path(os.getenv("BOTTAZZI_CALL_RECORDINGS_DIR", "/var/lib/ralfloop-bottazzi-call-recordings"))
    state = Path(os.getenv("BOTTAZZI_CALL_AGENDA_STATE", "/var/lib/ralfloop-bottazzi-call-agenda/state.json"))
    memory = Path(os.getenv("RALFLOOP_OPERATIONAL_MEMORY_PATH", str(Path.home() / ".local/share/bottazzi/runtime-production/operational-memory.sqlite3")))
    return recordings, state, memory


def _run_once() -> int:
    recordings, state, memory_path = _paths()
    with MemoryService(memory_path) as memory:
        trigger = CallAgendaTrigger(
            recordings_root=recordings,
            agenda_intake=build_default_agenda_intake(memory),
            state_path=state,
        )
        result = trigger.poll()
    if result.ready_seen or result.missing_transcript:
        print(json.dumps(result.__dict__, sort_keys=True), flush=True)
    return result.ingested


def main() -> int:
    args = _parser().parse_args()
    if args.once:
        _run_once()
        return 0
    interval = max(2.0, args.interval)
    while True:
        try:
            if _run_once() == 0:
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"call_agenda_direct_error={exc.__class__.__name__}", file=sys.stderr, flush=True)
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
