#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

try:
    from .passage_matcher import one_to_one_pairs
except ImportError:
    from passage_matcher import one_to_one_pairs

BASE = Path("/opt/bottazzi-presence")
CITOFONO_EVENTS = Path("/opt/bottazzi-citofono/events.jsonl")
GARDEN_EVENTS = Path("/opt/bottazzi-garden/events.jsonl")

OUT_EVENTS = BASE / "passage_events.jsonl"
STATE_FILE = BASE / "passage_state.json"

WINDOW_SECONDS = 180
PENDING_GRACE_SECONDS = 300


def parse_ts(value: str | None, event_id: str | None = None) -> datetime | None:
    if value:
        try:
            return datetime.fromisoformat(value).replace(tzinfo=None)
        except Exception:
            pass
    if event_id:
        m = re.search(r"_(20\d{6})_(\d{6})_", event_id)
        if m:
            return datetime.strptime("".join(m.groups()), "%Y%m%d%H%M%S")
    return None


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: Path, data):
    BASE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_jsonl(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def emit(event: dict):
    BASE.mkdir(parents=True, exist_ok=True)
    with OUT_EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def is_citofono_passage(ev: dict) -> bool:
    return ev.get("event") == "camera_wake_face_burst"


def is_garden_passage(ev: dict) -> bool:
    return ev.get("event") == "human_passage"


def nearest_garden(citofono_ts: datetime):
    best = None
    for ev in load_jsonl(GARDEN_EVENTS):
        if not is_garden_passage(ev):
            continue
        gt = parse_ts(ev.get("ts"), ev.get("event_id"))
        if not gt:
            continue
        delta = (gt - citofono_ts).total_seconds()
        if abs(delta) > WINDOW_SECONDS:
            continue
        if best is None or abs(delta) < abs(best["delta_seconds"]):
            best = {"event": ev, "garden_ts": gt, "delta_seconds": delta}
    return best


def direction_from_delta(delta_seconds: float):
    # citofono = lato strada, giardino = lato interno.
    # citofono prima + giardino dopo => entrata.
    # giardino prima + citofono dopo => uscita.
    if delta_seconds > 0:
        return "entrata_probabile", "inside", +1
    return "uscita_probabile", "outside", -1


def confidence_from_delta(delta_seconds: float, garden_conf=None):
    ad = abs(delta_seconds)
    base = "high" if ad <= 75 else "medium" if ad <= 140 else "low"
    try:
        gc = float(garden_conf)
    except Exception:
        gc = None

    if gc is not None and gc < 0.45 and base == "high":
        return "medium"
    if gc is not None and gc < 0.35:
        return "low"
    return base


def make_track_id(citofono_event_id: str, garden_event_id: str | None):
    raw = f"{citofono_event_id}|{garden_event_id or ''}"
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    m = re.search(r"citofono_(20\d{6})_(\d{6})_", citofono_event_id or "")
    if m:
        stamp = "".join(m.groups())
        return f"passage_{stamp}_{h}"
    return f"passage_{h}"


def process(limit: int = 200):
    state = load_json(STATE_FILE, {
        "processed_citofono": [],
        "pending_citofono": [],
        "claimed_garden_events": [],
        "unknown_inside_balance": 0,
        "last_run": None,
    })
    processed = set(state.setdefault("processed_citofono", []))
    pending = set(state.setdefault("pending_citofono", []))
    claimed = set(state.setdefault("claimed_garden_events", []))

    # Migration: recover garden ids already consumed by the legacy tracker.
    for old in load_jsonl(OUT_EVENTS):
        gid = old.get("garden_event_id")
        if old.get("event") == "physical_passage_tracked" and gid:
            claimed.add(gid)

    citofono_all = [ev for ev in load_jsonl(CITOFONO_EVENTS) if is_citofono_passage(ev)]
    by_id = {ev.get("event_id"): ev for ev in citofono_all if ev.get("event_id")}
    recent_ids = [ev.get("event_id") for ev in citofono_all[-limit:] if ev.get("event_id")]
    candidate_ids = (pending | set(recent_ids)) - processed
    candidates = [by_id[cid] for cid in candidate_ids if cid in by_id]
    gardens = [ev for ev in load_jsonl(GARDEN_EVENTS) if is_garden_passage(ev)]
    pairs = one_to_one_pairs(candidates, gardens, WINDOW_SECONDS, claimed)
    paired = {row["citofono"].get("event_id"): row for row in pairs}

    now = datetime.now()
    changed = False
    for ev in sorted(candidates, key=lambda x: parse_ts(x.get("ts"), x.get("event_id")) or now):
        cid = ev.get("event_id")
        cts = parse_ts(ev.get("ts"), cid)
        if not cid or not cts:
            continue
        pair = paired.get(cid)
        if pair is None:
            age = max(0.0, (now - cts).total_seconds())
            if age < PENDING_GRACE_SECONDS:
                pending.add(cid)
                continue
            event = {
                "source": "passage_tracker", "event": "physical_passage_unpaired",
                "ts": now.isoformat(timespec="seconds"), "track_id": make_track_id(cid, None),
                "identity_status": "unknown_pending_manual_review",
                "direction_hint": "unknown_direction", "presence_state": "unknown",
                "confidence": "low", "citofono_event_id": cid,
                "citofono_ts": cts.isoformat(timespec="seconds"),
                "frames_count": len(ev.get("frames") or []),
                "note": "nessun passaggio giardino one-to-one entro finestra dopo grace",
            }
            emit(event); processed.add(cid); pending.discard(cid); changed = True
            continue

        gev = pair["garden"]; gid = gev.get("event_id"); delta = float(pair["delta_seconds"])
        direction, presence_state, balance_delta = direction_from_delta(delta)
        gts = parse_ts(gev.get("ts"), gid)
        event = {
            "source": "passage_tracker", "event": "physical_passage_tracked",
            "ts": now.isoformat(timespec="seconds"), "track_id": make_track_id(cid, gid),
            "identity_status": "unknown_pending_late_face_or_manual_review",
            "direction_hint": direction, "presence_state": presence_state,
            "occupancy_delta": balance_delta,
            "confidence": confidence_from_delta(delta, gev.get("best_confidence")),
            "citofono_event_id": cid, "citofono_ts": cts.isoformat(timespec="seconds"),
            "garden_event_id": gid, "garden_ts": gts.isoformat(timespec="seconds") if gts else gev.get("ts"),
            "garden_delta_seconds": round(delta, 1), "garden_confidence": gev.get("best_confidence"),
            "snapshot": gev.get("snapshot"), "garden_clip": gev.get("clip"),
            "frames_count": len(ev.get("frames") or []),
            "matching": "one_to_one_min_time_delta",
        }
        state["unknown_inside_balance"] = max(0, int(state.get("unknown_inside_balance", 0)) + balance_delta)
        emit(event); processed.add(cid); pending.discard(cid); claimed.add(gid); changed = True

    state["processed_citofono"] = sorted(processed)[-5000:]
    state["pending_citofono"] = sorted(pending)[-1000:]
    state["claimed_garden_events"] = sorted(claimed)[-10000:]
    state["last_run"] = now.isoformat(timespec="seconds")
    if changed or candidates:
        save_json(STATE_FILE, state)
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    process(args.limit)


if __name__ == "__main__":
    main()
