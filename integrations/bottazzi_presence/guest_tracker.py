#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from insightface.app import FaceAnalysis

BASE = Path("/opt/bottazzi-presence")
CITOFONO_EVENTS = Path("/opt/bottazzi-citofono/events.jsonl")
GARDEN_EVENTS = Path("/opt/bottazzi-garden/events.jsonl")
REGISTRY = BASE / "guest_registry.json"
EVENTS = BASE / "guest_events.jsonl"
PASSAGE_EVENTS = BASE / "passage_events.jsonl"
KNOWN_DB = Path("/home/sibilla-cumana/bottazzi-face/faces_insight.json")

KNOWN_THRESHOLD = 0.42
GUEST_THRESHOLD = 0.48
MIN_DET_SCORE = 0.45
WINDOW_SECONDS = 180


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


def cosine(a, b) -> float:
    return float(np.dot(a, b) / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-9))


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


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


def save_registry(data):
    BASE.mkdir(parents=True, exist_ok=True)
    REGISTRY.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def emit(event):
    BASE.mkdir(parents=True, exist_ok=True)
    with EVENTS.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def load_known():
    data = load_json(KNOWN_DB, {"people": {}})
    rows = []
    for name, items in data.get("people", {}).items():
        for item in items:
            rows.append((name, np.array(item["embedding"], dtype="float32")))
    return rows


def app():
    face_app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
    face_app.prepare(ctx_id=-1, det_size=(640, 640))
    return face_app


def best_unknown_embedding(face_app, frames: list[str], known) -> dict | None:
    best = None
    for frame in frames:
        img = cv2.imread(frame)
        if img is None:
            continue
        for face in face_app.get(img):
            if float(face.det_score) < MIN_DET_SCORE:
                continue
            emb = face.embedding.astype("float32")
            known_scores = [{"name": n, "score": cosine(emb, e)} for n, e in known]
            known_best = max(known_scores, key=lambda x: x["score"], default={"name": None, "score": 0.0})
            item = {
                "embedding": emb,
                "frame": frame,
                "det_score": float(face.det_score),
                "bbox": [round(float(x), 1) for x in face.bbox.tolist()],
                "known_best_name": known_best["name"],
                "known_best_score": float(known_best["score"]),
            }
            if known_best["score"] >= KNOWN_THRESHOLD:
                continue
            if best is None or item["det_score"] > best["det_score"]:
                best = item
    return best



def is_ignored_guest(guest: dict) -> bool:
    return (
        guest.get("ignored") is True
        or guest.get("state") == "ignored"
        or guest.get("verdict") == "false_positive_rejected"
        or guest.get("last_direction") == "false_positive_rejected"
    )


def assign_guest(reg, emb) -> tuple[str, float]:
    guests = reg.setdefault("guests", {})
    best_id, best_score = None, -1.0
    for guest_id, guest in guests.items():
        if is_ignored_guest(guest):
            continue
        for row in guest.get("embeddings", []):
            score = cosine(emb, np.array(row, dtype="float32"))
            if score > best_score:
                best_id, best_score = guest_id, score
    if best_id and best_score >= GUEST_THRESHOLD:
        return best_id, best_score
    next_num = 1 + max([int(x.split("_")[1]) for x in guests if re.match(r"guest_\d+$", x)] or [0])
    return f"guest_{next_num:03d}", best_score


def update_guest(reg, guest_id, emb, citofono_event, face):
    guests = reg.setdefault("guests", {})
    guest = guests.setdefault(guest_id, {
        "label": guest_id,
        "type": "unknown_airbnb_guest",
        "state": "unknown",
        "embeddings": [],
        "first_seen": None,
        "last_seen": None,
        "events": [],
    })
    if len(guest["embeddings"]) < 12:
        guest["embeddings"].append([float(x) for x in emb.tolist()])
    ts = parse_ts(citofono_event.get("ts"), citofono_event.get("event_id"))
    ts_s = ts.isoformat(timespec="seconds") if ts else datetime.now().isoformat(timespec="seconds")
    guest["first_seen"] = guest.get("first_seen") or ts_s
    guest["last_seen"] = ts_s
    guest["sample_frame"] = face["frame"]
    guest["last_face"] = {
        "frame": face["frame"],
        "det_score": round(face["det_score"], 4),
        "known_best_name": face["known_best_name"],
        "known_best_score": round(face["known_best_score"], 4),
    }
    return guest


def nearest_garden(citofono_ts: datetime):
    best = None
    for ev in load_jsonl(GARDEN_EVENTS):
        if ev.get("event") != "human_passage":
            continue
        gt = parse_ts(ev.get("ts"), ev.get("event_id"))
        if not gt:
            continue
        delta = (gt - citofono_ts).total_seconds()
        if abs(delta) > WINDOW_SECONDS:
            continue
        if best is None or abs(delta) < abs(best["delta_seconds"]):
            best = {"event": ev, "delta_seconds": delta}
    return best



def passage_for_citofono(citofono_event_id: str | None) -> dict | None:
    if not citofono_event_id:
        return None
    for ev in reversed(load_jsonl(PASSAGE_EVENTS)):
        if ev.get("citofono_event_id") == citofono_event_id and ev.get("event") == "physical_passage_tracked":
            return ev
    return None


def claimed_passage_for_citofono(citofono_event_id: str | None) -> dict | None:
    if not citofono_event_id:
        return None

    for ev in reversed(load_jsonl(PASSAGE_EVENTS)):
        if ev.get("citofono_event_id") != citofono_event_id:
            continue

        if ev.get("identity_status") == "known_claim_linked":
            return ev

        claim = ev.get("presence_claim") or {}
        if claim.get("source") in ("manual_user_assertion", "presence_correlator"):
            return ev

    return None


def process(limit: int = 80):
    reg = load_json(REGISTRY, {"guests": {}, "processed": []})
    processed = set(reg.setdefault("processed", []))
    known = load_known()
    face_app = app()
    changed = False
    for ev in load_jsonl(CITOFONO_EVENTS)[-limit:]:
        if ev.get("event") != "camera_wake_face_burst":
            continue
        event_id = ev.get("event_id")
        if not event_id or event_id in processed:
            continue
        claim = claimed_passage_for_citofono(event_id)
        if claim:
            emit({
                "source": "guest_tracker",
                "event": "guest_skipped_known_passage_claim",
                "ts": datetime.now().isoformat(timespec="seconds"),
                "citofono_event_id": event_id,
                "track_id": claim.get("track_id"),
                "presence_state": claim.get("presence_state"),
                "direction_hint": claim.get("direction_hint"),
                "reason": claim.get("reason"),
                "presence_claim": claim.get("presence_claim"),
            })
            processed.add(event_id)
            changed = True
            continue

        frames = ev.get("frames") or []
        face = best_unknown_embedding(face_app, frames, known)
        if not face:
            processed.add(event_id)
            continue
        guest_id, match_score = assign_guest(reg, face["embedding"])
        guest = update_guest(reg, guest_id, face["embedding"], ev, face)
        ts = parse_ts(ev.get("ts"), event_id) or datetime.now()
        passage = passage_for_citofono(event_id)
        direction = "passaggio_sconosciuto"
        state = guest.get("state", "unknown")
        if passage:
            direction = passage.get("direction_hint") or direction
            state = passage.get("presence_state") or state
        event = {
            "source": "guest_tracker",
            "event": "guest_presence_inferred",
            "ts": datetime.now().isoformat(timespec="seconds"),
            "guest_id": guest_id,
            "direction_hint": direction,
            "presence_state": state,
            "citofono_event_id": event_id,
            "citofono_ts": ts.isoformat(timespec="seconds"),
            "sample_frame": face["frame"],
            "known_best_name": face["known_best_name"],
            "known_best_score": round(face["known_best_score"], 4),
            "guest_match_score": round(match_score, 4),
        }
        if passage:
            event.update({
                "track_id": passage.get("track_id"),
                "garden_event_id": passage.get("garden_event_id"),
                "garden_ts": passage.get("garden_ts"),
                "garden_delta_seconds": passage.get("garden_delta_seconds"),
                "snapshot": passage.get("snapshot"),
                "garden_clip": passage.get("garden_clip"),
            })
        guest["state"] = state
        guest["last_direction"] = direction
        guest["events"].append(event)
        guest["events"] = guest["events"][-100:]
        emit(event)
        processed.add(event_id)
        changed = True
    reg["processed"] = list(processed)[-2000:]
    if changed:
        save_registry(reg)
    return changed


def manual(guest_id: str, direction: str, when: str, note: str):
    reg = load_json(REGISTRY, {"guests": {}, "processed": []})
    guest = reg.setdefault("guests", {}).setdefault(guest_id, {
        "label": guest_id,
        "type": "unknown_airbnb_guest",
        "state": "unknown",
        "embeddings": [],
        "events": [],
    })
    state = "outside" if direction.startswith("uscita") else "inside"
    ev = {
        "source": "manual_user_assertion",
        "event": "guest_presence_manual",
        "ts": datetime.now().isoformat(timespec="seconds"),
        "guest_id": guest_id,
        "direction_hint": direction,
        "presence_state": state,
        "observed_ts": when,
        "note": note,
    }
    guest["state"] = state
    guest["last_seen"] = when
    guest["last_direction"] = direction
    guest.setdefault("events", []).append(ev)
    emit(ev)
    save_registry(reg)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=80)
    parser.add_argument("--manual-guest")
    parser.add_argument("--direction")
    parser.add_argument("--when")
    parser.add_argument("--note", default="")
    args = parser.parse_args()
    if args.manual_guest:
        manual(args.manual_guest, args.direction or "passaggio_manual", args.when or datetime.now().isoformat(timespec="seconds"), args.note)
    else:
        process(args.limit)


if __name__ == "__main__":
    main()
