#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from ralfloop_agent.abc_relation.client import DEFAULT_SOCKET, RelationMCPClient
from ralfloop_agent.abc_relation.intake import (
    normalize_event,
    proposal_digest,
    require_confirmed_digest,
)

def load_events(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping):
        payload = payload.get("events")
    if not isinstance(payload, list) or not payload:
        raise ValueError("input must be a non-empty JSON array or {'events': [...]}")
    if not all(isinstance(row, Mapping) for row in payload):
        raise ValueError("every event must be a JSON object")
    return [normalize_event(row) for row in payload]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate or commit normalized ABC Relation events."
    )
    parser.add_argument("input", type=Path, help="JSON file containing normalized events")
    parser.add_argument("--socket", default=DEFAULT_SOCKET)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="Write through abc_record_event; without this flag the command is dry-run only.",
    )
    parser.add_argument(
        "--confirm-digest",
        help="Exact proposal_digest emitted by the matching dry-run; required with --commit.",
    )
    args = parser.parse_args()
    events = load_events(args.input)
    digest = proposal_digest(events)

    output: dict[str, Any] = {
        "mode": "commit" if args.commit else "dry_run",
        "validated": len(events),
        "proposal_digest": digest,
    }
    if args.commit:
        try:
            require_confirmed_digest(events, args.confirm_digest)
        except ValueError as exc:
            parser.error(str(exc))
        client = RelationMCPClient(args.socket)
        recorded = [client.call("abc_record_event", event)["event"] for event in events]
        output["recorded_event_ids"] = [row["event_id"] for row in recorded]
        output["analysis"] = client.analyze()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
