#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_SOURCES = {
    "passage": Path("/opt/bottazzi-presence/passage_events.jsonl"),
    "presence": Path("/opt/bottazzi-presence/presence_events.jsonl"),
    "guest": Path("/opt/bottazzi-presence/guest_events.jsonl"),
    "garden": Path("/opt/bottazzi-garden/events.jsonl"),
    "citofono": Path("/opt/bottazzi-citofono/events.jsonl"),
    "domotics": Path("/var/lib/ralf-domotics-watchdog/events.jsonl"),
}

ALLOWED_EVENTS = {
    "passage": {"physical_passage_tracked", "physical_passage_unpaired"},
    "presence": {"presence_inferred", "presence_exit_confirmed", "presence_short_roundtrip", "presence_confirmed"},
    "guest": {"guest_presence_inferred", "guest_skipped_known_passage_claim"},
    "garden": {"human_passage"},
    "citofono": {"garden_gate_small_motion_citofono_capture"},
    "domotics": {"DOMOTICS_ENTITY_UNAVAILABLE", "DOMOTICS_ENTITY_RECOVERED", "DOMOTICS_ENTITY_MISSING", "DOMOTICS_ENTITY_RETURNED"},
}

SAFE_FIELDS = (
    "ts", "source", "event", "event_id", "track_id", "direction_hint", "presence_state",
    "name", "guest_id", "citofono_event_id", "garden_event_id", "garden_delta_seconds",
    "confidence", "decision", "authorized", "reason", "entity_id", "site",
)


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def save_state(path: Path, state: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(state), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def normalize(kind: str, row: Mapping[str, Any]) -> dict[str, Any] | None:
    event = str(row.get("event") or "")
    if event not in ALLOWED_EVENTS.get(kind, set()):
        return None
    payload = {key: row[key] for key in SAFE_FIELDS if key in row}
    source_id = str(row.get("event_id") or row.get("track_id") or row.get("entity_id") or "")
    basis = json.dumps({"kind": kind, "source_id": source_id, "payload": payload}, sort_keys=True, separators=(",", ":"), default=str)
    return {
        "event_id": "activity_" + hashlib.sha256(basis.encode()).hexdigest()[:24],
        "event_type": event.upper(),
        "source_kind": kind,
        "source_id": source_id,
        "occurred_at": row.get("ts"),
        "payload": payload,
    }


def source_checkpoint(path: Path, offset: int) -> dict[str, Any] | None:
    try:
        st = path.stat()
        if offset < 0 or offset > st.st_size:
            return None
        start = max(0, offset - 256)
        with path.open("rb") as fh:
            fh.seek(start)
            sample = fh.read(offset - start)
        return {
            "dev": int(st.st_dev),
            "ino": int(st.st_ino),
            "offset": int(offset),
            "tail_sha256": hashlib.sha256(sample).hexdigest()[:24],
        }
    except OSError:
        return None


def checkpoint_matches(path: Path, checkpoint: Mapping[str, Any] | None) -> bool:
    if not checkpoint:
        return True
    current = source_checkpoint(path, int(checkpoint.get("offset") or 0))
    return bool(current and current == dict(checkpoint))


def process_source(kind: str, path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    if not path.exists():
        return 0, []
    size = path.stat().st_size
    if offset < 0 or offset > size:
        offset = 0
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        for line in fh:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(raw, Mapping):
                normalized = normalize(kind, raw)
                if normalized is not None:
                    rows.append(normalized)
        return fh.tell(), rows


def append_rows(path: Path, rows: Iterable[Mapping[str, Any]]) -> int:
    data = list(rows)
    if not data:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in data:
            fh.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(data)


def run(sources: Mapping[str, Path], state_path: Path, out_path: Path, initialize_only: bool = False) -> dict[str, Any]:
    previous = load_state(state_path)
    initialized = bool(previous.get("initialized"))
    offsets = dict(previous.get("offsets") or {})
    checkpoints = dict(previous.get("checkpoints") or {})
    seen_ids = list(previous.get("seen_ids") or [])[-16384:]
    seen = set(str(x) for x in seen_ids)
    emitted: list[dict[str, Any]] = []
    next_offsets: dict[str, int] = {}
    next_checkpoints: dict[str, dict[str, Any]] = {}
    for kind, path in sources.items():
        previous_offset = int(offsets.get(kind) or 0)
        start_offset = previous_offset
        if not initialized:
            start_offset = 0
        elif not checkpoint_matches(path, checkpoints.get(kind)):
            start_offset = 0
        new_offset, rows = process_source(kind, path, start_offset)
        next_offsets[kind] = new_offset
        cp = source_checkpoint(path, new_offset)
        if cp is not None:
            next_checkpoints[kind] = cp
        for row in rows:
            eid = str(row.get("event_id") or "")
            if not eid or eid in seen:
                continue
            seen.add(eid)
            seen_ids.append(eid)
            if initialized:
                emitted.append(row)
    if initialized and not initialize_only:
        append_rows(out_path, emitted)
    state = {
        "initialized": True,
        "offsets": next_offsets,
        "checkpoints": next_checkpoints,
        "seen_ids": seen_ids[-16384:],
        "last_emitted": len(emitted) if initialized else 0,
    }
    save_state(state_path, state)
    return {"status": "baseline_initialized" if not initialized else "completed", "emitted": 0 if not initialized else len(emitted), "sources": len(sources)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="/var/lib/bottazzi-activity-correlator/state.json")
    parser.add_argument("--out", default="/var/lib/bottazzi-activity-correlator/events.jsonl")
    parser.add_argument("--initialize-only", action="store_true")
    args = parser.parse_args()
    result = run(DEFAULT_SOURCES, Path(args.state), Path(args.out), args.initialize_only)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
