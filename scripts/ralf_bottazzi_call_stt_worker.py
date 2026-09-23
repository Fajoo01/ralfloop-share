#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time

from ralfloop_agent.call_recordings import CallRecordingStore
from ralfloop_agent.call_stt import FasterWhisperTranscriber, transcribe_pending


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bot-tazzi offline call-recording STT worker")
    parser.add_argument("--once", action="store_true", help="Process pending recordings once and exit")
    parser.add_argument("--interval", type=float, default=15.0, help="Idle polling interval in seconds")
    parser.add_argument("--limit", type=int, default=4, help="Maximum recordings per pass")
    return parser


def _run_once(store: CallRecordingStore, transcriber: FasterWhisperTranscriber, limit: int) -> int:
    rows = transcribe_pending(store, transcriber, limit=max(1, limit))
    if rows:
        print(json.dumps({"processed": len(rows), "recordings": [r.get("recording_id") for r in rows]}), flush=True)
    return len(rows)


def main() -> int:
    args = _parser().parse_args()
    store = CallRecordingStore.from_env()
    transcriber = FasterWhisperTranscriber.from_env()
    if args.once:
        _run_once(store, transcriber, args.limit)
        return 0

    interval = max(2.0, args.interval)
    while True:
        try:
            processed = _run_once(store, transcriber, args.limit)
            if processed == 0:
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(f"call_stt_worker_error={exc.__class__.__name__}", file=sys.stderr, flush=True)
            time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
