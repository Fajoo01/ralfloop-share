#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from ralfloop_agent.abc_relation.client import DEFAULT_SOCKET, RelationMCPClient
from ralfloop_agent.abc_relation.models import EvidenceKind, SourceKind

ALLOWED_FIELDS = {
    "occurred_at", "kind", "summary", "source_kind", "source_ref", "actor",
    "confidence", "weight", "tags", "raw_excerpt", "source_hash",
    "supersedes_event_id",
}
REQUIRED_FIELDS = {"occurred_at", "kind", "summary", "source_kind", "source_ref"}


def _aware_datetime(raw: str) -> str:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at must include timezone")
    return value.isoformat()


def normalize_event(row: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(row) - ALLOWED_FIELDS
    missing = REQUIRED_FIELDS - set(row)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing fields: {sorted(missing)}")

    normalized = dict(row)
    normalized["occurred_at"] = _aware_datetime(str(row["occurred_at"]))
    normalized["kind"] = EvidenceKind(str(row["kind"])).value
    normalized["source_kind"] = SourceKind(str(row["source_kind"])).value
    normalized["summary"] = str(row["summary"]).strip()
    normalized["source_ref"] = str(row["source_ref"]).strip()
    if not normalized["summary"] or not normalized["source_ref"]:
        raise ValueError("summary and source_ref must be non-empty")

    confidence = float(row.get("confidence", 1.0))
    weight = float(row.get("weight", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence out of range")
    if not -100.0 <= weight <= 100.0:
        raise ValueError("weight out of range")
    normalized["confidence"] = confidence
    normalized["weight"] = weight
    tags = row.get("tags", [])
    if not isinstance(tags, list):
        raise ValueError("tags must be a list")
    normalized["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]

    raw_excerpt = row.get("raw_excerpt")
    if raw_excerpt is not None and len(str(raw_excerpt)) > 600:
        raise ValueError("raw_excerpt exceeds 600 characters")
    if raw_excerpt is not None:
        normalized["raw_excerpt"] = str(raw_excerpt)

    actor = row.get("actor")
    if actor is not None:
        normalized["actor"] = str(actor)
    return normalized


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
    args = parser.parse_args()
    events = load_events(args.input)

    output: dict[str, Any] = {"mode": "commit" if args.commit else "dry_run", "validated": len(events)}
    if args.commit:
        client = RelationMCPClient(args.socket)
        recorded = [client.call("abc_record_event", event)["event"] for event in events]
        output["recorded_event_ids"] = [row["event_id"] for row in recorded]
        output["analysis"] = client.analyze()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
