#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

CERTAINTY_RANK = {"stale": 0, "uncertain": 1, "probable": 2, "confirmed": 3}
VALID_STATES = {"inside", "outside", "unknown", "pending_outside"}


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


def append_rows(path: Path, rows: list[Mapping[str, Any]]) -> int:
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(rows)


def source_checkpoint(path: Path, offset: int) -> dict[str, Any] | None:
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
    return source_checkpoint(path, int(saved.get("offset") or 0)) == dict(saved)


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


def bootstrap_people(legacy_path: Path) -> dict[str, dict[str, Any]]:
    legacy = load_json(legacy_path, {})
    people = {}
    for name, row in (legacy.get("people") or {}).items():
        if not isinstance(row, Mapping):
            continue
        key = str(name).strip().casefold()
        if not key:
            continue
        people[key] = {
            "state": "unknown",
            "certainty": "stale",
            "observed_at": None,
            "source_session_id": None,
            "direction_hint": None,
            "legacy_last_state": row.get("state"),
            "legacy_last_seen": row.get("last_seen"),
        }
    return people


def normalized_claim(claim: Mapping[str, Any]) -> dict[str, Any] | None:
    if str(claim.get("kind") or "") != "known":
        return None
    subject = str(claim.get("subject") or "").strip().casefold()
    state = str(claim.get("state") or "unknown").strip().casefold()
    certainty = str(claim.get("certainty") or "uncertain").strip().casefold()
    if not subject or state not in VALID_STATES or certainty not in CERTAINTY_RANK:
        return None
    if state == "pending_outside":
        state = "outside"
        certainty = "probable" if CERTAINTY_RANK[certainty] > 1 else certainty
    return {
        "subject": subject, "state": state, "certainty": certainty,
        "observed_at": claim.get("occurred_at"), "direction_hint": claim.get("direction_hint"),
        "event_type": claim.get("event_type"), "event_source": claim.get("event_source"),
        "evidence_event_id": claim.get("event_id"),
    }


def apply_session(registry: dict[str, Any], session: Mapping[str, Any]) -> list[dict[str, Any]]:
    if session.get("event") != "SITE_ACTIVITY_SESSION" or session.get("site") != "sede":
        return []
    session_id = str(session.get("event_id") or "")
    if not session_id:
        return []
    seen = registry.setdefault("seen_session_ids", [])
    if session_id in set(seen):
        return []
    changes = []
    people = registry.setdefault("people", {})
    for raw_claim in session.get("subject_claims") or []:
        if not isinstance(raw_claim, Mapping):
            continue
        claim = normalized_claim(raw_claim)
        if claim is None:
            continue
        subject = claim.pop("subject")
        before = dict(people.get(subject) or {"state": "unknown", "certainty": "stale"})
        after = {
            **before,
            **claim,
            "source_session_id": session_id,
        }
        people[subject] = after
        if before.get("state") != after.get("state") or before.get("certainty") != after.get("certainty"):
            changes.append({
                "event": "SEDE_PRESENCE_CHANGED", "site": "sede", "subject": subject,
                "before_state": before.get("state", "unknown"), "after_state": after["state"],
                "certainty": after["certainty"], "observed_at": after.get("observed_at"),
                "source_session_id": session_id, "direction_hint": after.get("direction_hint"),
            })
    observations = registry.setdefault("identity_observations", [])
    for hint in session.get("identity_hints") or []:
        if not isinstance(hint, Mapping):
            continue
        subject = str(hint.get("subject") or "").strip().casefold()
        if not subject:
            continue
        observations.append({
            "subject": subject, "observed_at": hint.get("occurred_at"),
            "reason": hint.get("reason"), "support_frames": hint.get("support_frames"),
            "frame_ratio": hint.get("frame_ratio"), "source_session_id": session_id,
            "role": "identity_hint_only",
        })
    registry["identity_observations"] = observations[-256:]
    anonymous = registry.setdefault("anonymous", {"direction_balance": 0, "unresolved_count": 0, "movements": []})
    claims = [c for c in (session.get("subject_claims") or []) if isinstance(c, Mapping)]
    has_known_claim = any(str(c.get("kind") or "") == "known" for c in claims)
    if not has_known_claim:
        direction = str(session.get("direction_hint") or "")
        presence = str(session.get("presence_state") or "")
        confidence = str(session.get("movement_confidence") or "").casefold()
        delta = 0
        if "rientro" in direction or presence == "inside": delta = 1
        elif "uscita" in direction or presence == "outside": delta = -1
        if delta and confidence in {"high", "medium"}:
            anonymous["direction_balance"] = int(anonymous.get("direction_balance") or 0) + delta
            certainty = "probable"
        else:
            anonymous["unresolved_count"] = int(anonymous.get("unresolved_count") or 0) + 1
            certainty = "uncertain"
        movement = {
            "session_id": session_id, "ended_at": session.get("ended_at"),
            "direction_hint": direction or None, "presence_state": presence or None,
            "certainty": certainty, "delta": delta if certainty == "probable" else 0,
        }
        anonymous["movements"] = (list(anonymous.get("movements") or []) + [movement])[-128:]
    seen.append(session_id)
    registry["seen_session_ids"] = seen[-32768:]
    return changes


def run(source: Path, state_path: Path, events_path: Path, legacy_path: Path) -> dict[str, Any]:
    registry = load_json(state_path, {})
    initialized = bool(registry.get("initialized"))
    if not initialized:
        offset = source.stat().st_size if source.exists() else 0
        registry = {
            "schema_version": 1, "site": "sede", "initialized": True,
            "offset": offset, "checkpoint": source_checkpoint(source, offset),
            "people": bootstrap_people(legacy_path),
            "anonymous": {"direction_balance": 0, "unresolved_count": 0, "movements": []},
            "seen_session_ids": [],
            "identity_observations": [],
            "note": "Current state starts unknown; legacy presence is historical context only. Face identity hints are observational and never change presence alone.",
        }
        atomic_json(state_path, registry)
        return {"status": "baseline_initialized", "processed": 0, "changes": 0, "people": len(registry["people"])}
    offset = int(registry.get("offset") or 0)
    if not checkpoint_matches(source, registry.get("checkpoint")):
        offset = 0
    new_offset, rows = read_new(source, offset)
    changes = []
    processed = 0
    for row in rows:
        if row.get("event") != "SITE_ACTIVITY_SESSION" or row.get("site") != "sede":
            continue
        processed += 1
        changes.extend(apply_session(registry, row))
    registry["offset"] = new_offset
    registry["checkpoint"] = source_checkpoint(source, new_offset)
    append_rows(events_path, changes)
    atomic_json(state_path, registry)
    return {"status": "completed", "processed": processed, "changes": len(changes), "people": len(registry.get("people") or {})}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="/var/lib/bottazzi-site-correlator/events.jsonl")
    ap.add_argument("--state", default="/var/lib/bottazzi-sede-presence/registry.json")
    ap.add_argument("--events", default="/var/lib/bottazzi-sede-presence/events.jsonl")
    ap.add_argument("--legacy", default="/opt/bottazzi-presence/registry.json")
    args = ap.parse_args()
    result = run(Path(args.source), Path(args.state), Path(args.events), Path(args.legacy))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__": raise SystemExit(main())
