#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ACTIVITY_EVENT_TYPES = {
    "HUMAN_PASSAGE", "GARDEN_GATE_SMALL_MOTION_CITOFONO_CAPTURE",
    "PHYSICAL_PASSAGE_TRACKED", "PHYSICAL_PASSAGE_UNPAIRED",
    "PRESENCE_INFERRED", "PRESENCE_EXIT_CONFIRMED", "PRESENCE_SHORT_ROUNDTRIP", "PRESENCE_CONFIRMED",
    "GUEST_PRESENCE_INFERRED", "GUEST_SKIPPED_KNOWN_PASSAGE_CLAIM",
    "HA_STATE_CHANGED", "FACE_IDENTITY_HINT",
}


def load_json(path: Path, default):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def parse_ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


def checkpoint(path: Path, offset: int) -> dict[str, Any] | None:
    try:
        st = path.stat()
        if offset < 0 or offset > st.st_size:
            return None
        start = max(0, offset - 256)
        with path.open("rb") as fh:
            fh.seek(start); sample = fh.read(offset - start)
        return {"dev": int(st.st_dev), "ino": int(st.st_ino), "offset": int(offset), "tail": hashlib.sha256(sample).hexdigest()[:24]}
    except OSError:
        return None


def checkpoint_matches(path: Path, saved: Mapping[str, Any] | None) -> bool:
    if not saved:
        return True
    return checkpoint(path, int(saved.get("offset") or 0)) == dict(saved)


def read_new(path: Path, offset: int) -> tuple[int, list[dict[str, Any]]]:
    if not path.exists():
        return 0, []
    if offset < 0 or offset > path.stat().st_size:
        offset = 0
    rows = []
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(offset)
        for line in fh:
            try: row = json.loads(line)
            except json.JSONDecodeError: continue
            if isinstance(row, dict): rows.append(row)
        return fh.tell(), rows


def activity_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    event_type = str(row.get("event_type") or "")
    site = str(row.get("site") or (row.get("payload") or {}).get("site") or "unassigned")
    ts = parse_ts(row.get("occurred_at"))
    if event_type not in ACTIVITY_EVENT_TYPES or site == "unassigned" or ts is None:
        return None
    payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
    return {
        "event_id": str(row.get("event_id") or ""), "event_type": event_type,
        "source_id": str(row.get("source_id") or ""),
        "source_kind": str(row.get("source_kind") or ""), "site": site,
        "occurred_at": str(row.get("occurred_at") or ""), "epoch": ts,
        "direction_hint": payload.get("direction_hint"), "presence_state": payload.get("presence_state"),
        "name": payload.get("name"), "guest_id": payload.get("guest_id"),
        "confidence": payload.get("confidence"), "event_source": payload.get("source"),
        "entity_id": payload.get("entity_id"), "old_state": payload.get("old_state"), "new_state": payload.get("new_state"),
        "track_id": payload.get("track_id"), "citofono_event_id": payload.get("citofono_event_id"),
        "garden_event_id": payload.get("garden_event_id"),
        "identity_reason": payload.get("identity_reason"),
        "identity_support_frames": payload.get("identity_support_frames"),
        "identity_frame_ratio": payload.get("identity_frame_ratio"),
        "signal_role": payload.get("signal_role"),
        "auxiliary": payload.get("signal_role") == "auxiliary_relay" or event_type == "FACE_IDENTITY_HINT",
    }


def finalize_session(session: Mapping[str, Any]) -> dict[str, Any]:
    events = list(session.get("events") or [])
    ids = [str(x.get("event_id") or "") for x in events if x.get("event_id")]
    source_kinds = sorted({str(x.get("source_kind") or "") for x in events if x.get("source_kind")})
    event_types = sorted({str(x.get("event_type") or "") for x in events if x.get("event_type")})
    identity = json.dumps([session.get("site"), ids], separators=(",", ":"))
    latest_direction = next((x.get("direction_hint") for x in reversed(events) if x.get("direction_hint")), None)
    latest_presence = next((x.get("presence_state") for x in reversed(events) if x.get("presence_state")), None)
    subject_claims = []
    for event in events:
        name = str(event.get("name") or "").strip()
        guest_id = str(event.get("guest_id") or "").strip()
        if not name and not guest_id:
            continue
        etype = str(event.get("event_type") or "")
        if name:
            if etype in {"PRESENCE_CONFIRMED", "PRESENCE_EXIT_CONFIRMED"}:
                certainty = "confirmed"
            elif etype in {"PRESENCE_INFERRED", "PRESENCE_SHORT_ROUNDTRIP"}:
                certainty = "probable"
            else:
                continue
        else:
            if etype != "GUEST_PRESENCE_INFERRED":
                continue
            certainty = "uncertain"
        subject_claims.append({
            "kind": "known" if name else "guest",
            "subject": name or guest_id,
            "state": event.get("presence_state") or "unknown",
            "certainty": certainty,
            "event_type": etype,
            "event_source": event.get("event_source"),
            "event_id": event.get("event_id"),
            "occurred_at": event.get("occurred_at"),
            "direction_hint": event.get("direction_hint"),
        })
    identity_hints = []
    for event in events:
        if event.get("event_type") != "FACE_IDENTITY_HINT":
            continue
        subject = str(event.get("name") or "").strip().casefold()
        if not subject:
            continue
        identity_hints.append({
            "subject": subject, "occurred_at": event.get("occurred_at"),
            "reason": event.get("identity_reason"),
            "support_frames": event.get("identity_support_frames"),
            "frame_ratio": event.get("identity_frame_ratio"),
            "event_id": event.get("event_id"),
            "citofono_event_id": event.get("citofono_event_id") or event.get("source_id"),
            "role": "identity_hint_only",
        })
    face_by_citofono = {}
    for event in events:
        if event.get("event_type") != "FACE_IDENTITY_HINT":
            continue
        cid = str(event.get("citofono_event_id") or event.get("source_id") or "").strip()
        if cid and event.get("name"):
            face_by_citofono[cid] = event
    identity_passage_links = []
    for event in events:
        if event.get("event_type") != "PHYSICAL_PASSAGE_TRACKED":
            continue
        cid = str(event.get("citofono_event_id") or "").strip()
        face = face_by_citofono.get(cid)
        if not face:
            continue
        identity_passage_links.append({
            "event": "IDENTITY_PASSAGE_LINKED",
            "subject": str(face.get("name") or "").strip().casefold(),
            "citofono_event_id": cid,
            "track_id": event.get("track_id"),
            "direction_hint": event.get("direction_hint"),
            "presence_state": event.get("presence_state"),
            "movement_confidence": event.get("confidence"),
            "face_reason": face.get("identity_reason"),
            "face_support_frames": face.get("identity_support_frames"),
            "face_frame_ratio": face.get("identity_frame_ratio"),
            "face_event_id": face.get("event_id"),
            "passage_event_id": event.get("event_id"),
            "role": "probable_identity_for_tracked_passage",
        })
    movement_events = [x for x in events if not x.get("auxiliary")]
    movement_confidence = next((x.get("confidence") for x in reversed(movement_events) if x.get("confidence")), None)
    return {
        "event_id": "site_session_" + hashlib.sha256(identity.encode()).hexdigest()[:24],
        "event": "SITE_ACTIVITY_SESSION", "site": session.get("site"),
        "started_at": session.get("started_at"), "ended_at": session.get("last_at"),
        "event_count": len(events), "source_kinds": source_kinds, "event_types": event_types,
        "correlation": "multi_source" if len(source_kinds) >= 2 else "single_source",
        "direction_hint": latest_direction, "presence_state": latest_presence,
        "movement_confidence": movement_confidence,
        "subject_claims": subject_claims[-32:],
        "identity_hints": identity_hints[-32:],
        "identity_passage_links": identity_passage_links[-32:],
        "evidence_event_ids": ids[-64:],
    }


def append_rows(path: Path, rows: list[Mapping[str, Any]]) -> int:
    if not rows: return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows: fh.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(rows)


def run(source: Path, state_path: Path, out: Path, *, window_seconds: float = 180.0, now_epoch: float | None = None) -> dict[str, Any]:
    state = load_json(state_path, {})
    initialized = bool(state.get("initialized"))
    if not initialized:
        off = source.stat().st_size if source.exists() else 0
        cp = checkpoint(source, off)
        atomic_json(state_path, {"initialized": True, "offset": off, "checkpoint": cp, "open_sessions": {}, "pending_auxiliary": {}})
        return {"status": "baseline_initialized", "processed": 0, "finalized": 0, "open_sites": 0}
    offset = int(state.get("offset") or 0)
    if not checkpoint_matches(source, state.get("checkpoint")):
        offset = 0
    new_offset, raw_rows = read_new(source, offset)
    open_sessions = dict(state.get("open_sessions") or {})
    pending_auxiliary = dict(state.get("pending_auxiliary") or {})
    finalized: list[dict[str, Any]] = []
    processed = 0
    for raw in raw_rows:
        row = activity_row(raw)
        if row is None: continue
        processed += 1
        site = row["site"]
        current = open_sessions.get(site)
        if current and float(row["epoch"]) - float(current.get("last_epoch") or 0) > window_seconds:
            finalized.append(finalize_session(current)); current = None
        if row.get("auxiliary") and not current:
            pending = list(pending_auxiliary.get(site) or [])
            pending.append(row)
            pending_auxiliary[site] = pending[-32:]
            continue
        if not current:
            pending = [
                x for x in (pending_auxiliary.get(site) or [])
                if 0 <= float(row["epoch"]) - float(x.get("epoch") or 0) <= window_seconds
            ]
            current = {"site": site, "started_at": (pending[0]["occurred_at"] if pending else row["occurred_at"]), "last_at": row["occurred_at"], "last_epoch": row["epoch"], "events": pending}
            pending_auxiliary.pop(site, None)
        current["last_at"] = row["occurred_at"]; current["last_epoch"] = row["epoch"]
        current["events"] = (list(current.get("events") or []) + [row])[-64:]
        open_sessions[site] = current
    now = time.time() if now_epoch is None else float(now_epoch)
    for site, current in list(open_sessions.items()):
        if now - float(current.get("last_epoch") or now) > window_seconds:
            finalized.append(finalize_session(current)); del open_sessions[site]
    for site, pending in list(pending_auxiliary.items()):
        fresh = [x for x in pending if now - float(x.get("epoch") or 0) <= window_seconds]
        if fresh:
            pending_auxiliary[site] = fresh[-32:]
        else:
            pending_auxiliary.pop(site, None)
    append_rows(out, finalized)
    cp = checkpoint(source, new_offset)
    atomic_json(state_path, {"initialized": True, "offset": new_offset, "checkpoint": cp, "open_sessions": open_sessions, "pending_auxiliary": pending_auxiliary})
    return {"status": "completed", "processed": processed, "finalized": len(finalized), "open_sites": len(open_sessions)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="/var/lib/bottazzi-activity-correlator/events.jsonl")
    ap.add_argument("--state", default="/var/lib/bottazzi-site-correlator/state.json")
    ap.add_argument("--out", default="/var/lib/bottazzi-site-correlator/events.jsonl")
    ap.add_argument("--window-seconds", type=float, default=180.0)
    args = ap.parse_args()
    print(json.dumps(run(Path(args.source), Path(args.state), Path(args.out), window_seconds=args.window_seconds), ensure_ascii=False))
    return 0


if __name__ == "__main__": raise SystemExit(main())
