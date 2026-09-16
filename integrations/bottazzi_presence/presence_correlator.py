#!/usr/bin/env python3
import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

try:
    from .presence_logic import correlate_passage_face, should_process_passage
except ImportError:
    from presence_logic import correlate_passage_face, should_process_passage

GARDEN_EVENTS = Path("/opt/bottazzi-garden/events.jsonl")
CITOFONO_EVENTS = Path("/opt/bottazzi-citofono/events.jsonl")
OUT_JSONL = Path("/opt/bottazzi-presence/presence_events.jsonl")
STATE_FILE = Path("/opt/bottazzi-presence/state.json")
REGISTRY_FILE = Path("/opt/bottazzi-presence/registry.json")
PASSAGE_EVENTS = Path("/opt/bottazzi-presence/passage_events.jsonl")
WINDOW_SECONDS = 180
FACE_MAX_BEST_DISTANCE = 0.39
MIN_GARDEN_CONFIDENCE = 0.45
SHORT_ROUNDTRIP_SECONDS = 600

def parse_ts(s):
    return datetime.fromisoformat(s).replace(tzinfo=None)

def load_garden_events():
    out = []
    if not GARDEN_EVENTS.exists():
        return out
    for line in GARDEN_EVENTS.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("event") == "human_passage":
            out.append(ev)
    return out

def load_citofono_faces():
    out = []
    if CITOFONO_EVENTS.exists():
        for line in CITOFONO_EVENTS.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") != "camera_wake_face_burst":
                continue
            verdict = ev.get("verdict")
            if not verdict or verdict in {"nessun_volto_riconosciuto", "incerto"}:
                continue
            out.append({
                "source": "citofono",
                "event": "known_face",
                "name": verdict,
                "verdict": verdict,
                "consensus": ev.get("consensus"),
                "ts": ev.get("ts"),
                "event_id": ev.get("event_id"),
                "summary": ev.get("summary", []),
                "hits_count": ev.get("hits_count"),
                "frames_checked": ev.get("frames_checked"),
                "clip": ev.get("clip"),
                "clip_ok": ev.get("clip_ok"),
                "frames": ev.get("frames", []),
            })

    cmd = [
        "journalctl",
        "-u", "bottazzi-camera-wake-watcher",
        "--since", "12 hours ago",
        "--no-pager",
        "-o", "cat",
    ]
    cp = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    seen = {f"{x.get('event_id')}:{x.get('ts')}" for x in out}
    for line in cp.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("event") != "camera_wake_face_burst":
            continue
        verdict = ev.get("verdict")
        if not verdict or verdict in {"nessun_volto_riconosciuto", "incerto"}:
            continue
        key = f"{ev.get('event_id')}:{ev.get('ts')}"
        if key in seen:
            continue
        out.append({
            "source": "citofono",
            "event": "known_face",
            "name": verdict,
            "verdict": verdict,
            "consensus": ev.get("consensus"),
            "ts": ev.get("ts"),
            "event_id": ev.get("event_id"),
            "event_dir": ev.get("event_dir"),
            "summary": ev.get("summary", []),
            "hits_count": ev.get("hits_count"),
            "frames_checked": ev.get("frames_checked"),
        })
    return out

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"emitted": []}

def save_state(st):
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

def load_registry():
    if REGISTRY_FILE.exists():
        try:
            return json.loads(REGISTRY_FILE.read_text())
        except Exception:
            pass
    return {"people": {}, "events": []}

def save_registry(reg):
    REGISTRY_FILE.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")

def emit(ev):
    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")

def event_observed_time(ev):
    return parse_ts(ev.get("observed_ts") or ev.get("ts"))

def apply_event_to_registry(ev):
    reg = load_registry()
    person = reg.setdefault("people", {}).setdefault(ev.get("name"), {})
    pending = person.get("pending_exit")

    if ev.get("direction_hint") == "entrata_probabile" and pending:
        exit_ts = event_observed_time(pending)
        enter_ts = event_observed_time(ev)
        if exit_ts and enter_ts:
            seconds = (enter_ts - exit_ts).total_seconds()
            if 0 <= seconds <= SHORT_ROUNDTRIP_SECONDS:
                ev["event"] = "presence_short_roundtrip"
                ev["direction_hint"] = "uscita_rientro_breve"
                ev["short_roundtrip_seconds"] = round(seconds, 1)
        person.pop("pending_exit", None)
        ev["presence_state"] = "inside"

    if ev.get("direction_hint") == "uscita_probabile":
        ev["presence_state"] = "pending_outside"
        observed = event_observed_time(ev) or datetime.now()
        ev["pending_until"] = (observed + timedelta(seconds=SHORT_ROUNDTRIP_SECONDS)).isoformat(timespec="seconds")
        person["pending_exit"] = ev
        person.update({
            "state": "inside",
            "last_direction": "uscita_in_attesa_rientro",
            "last_seen": ev.get("observed_ts") or ev["ts"],
            "last_event": ev,
        })
    else:
        person.update({
            "state": ev.get("presence_state"),
            "last_direction": ev.get("direction_hint"),
            "last_seen": ev.get("observed_ts") or ev["ts"],
            "last_event": ev,
        })

    reg.setdefault("events", []).append(ev)
    reg["events"] = reg["events"][-500:]
    save_registry(reg)

def resolve_pending_exits():
    reg = load_registry()
    now = datetime.now()
    changed = False
    for name, person in reg.get("people", {}).items():
        pending = person.get("pending_exit")
        if not pending:
            continue
        until = parse_ts(pending.get("pending_until"))
        if until and now >= until:
            ev = dict(pending)
            ev.update({
                "event": "presence_exit_confirmed",
                "ts": now.isoformat(timespec="seconds"),
                "name": name,
                "direction_hint": "uscita_confermata_dopo_grace",
                "presence_state": "outside",
            })
            person.pop("pending_exit", None)
            person.update({
                "state": "outside",
                "last_direction": ev["direction_hint"],
                "last_seen": ev["ts"],
                "last_event": ev,
            })
            reg.setdefault("events", []).append(ev)
            emit(ev)
            changed = True
    if changed:
        reg["events"] = reg["events"][-500:]
        save_registry(reg)

def main():
    state = load_state()
    backfill = os.environ.get("BOTTAZZI_PRESENCE_BACKFILL", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not backfill and not state.get("v2_cutover_ts"):
        state["v2_cutover_ts"] = datetime.now().isoformat(timespec="seconds")
        state["v2_initialized"] = True
        save_state(state)
        print(json.dumps({"source":"presence_correlator_v2","event":"v2_cutover_initialized","cutover_ts":state["v2_cutover_ts"]}, ensure_ascii=False))
        return
    resolve_pending_exits()
    emitted = set(state.get("emitted", []))
    faces = {f.get("event_id"): f for f in load_citofono_faces() if f.get("event_id")}
    passages = []
    if PASSAGE_EVENTS.exists():
        for line in PASSAGE_EVENTS.read_text(errors="replace").splitlines():
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") == "physical_passage_tracked":
                passages.append(ev)

    for passage in passages[-5000:]:
        if not should_process_passage(passage, state.get("v2_cutover_ts"), backfill=backfill):
            continue
        face = faces.get(passage.get("citofono_event_id"))
        if not face:
            continue
        correlated = correlate_passage_face(passage, face)
        if not correlated:
            continue
        key = f'{passage.get("track_id")}:{correlated.get("name")}:v2'
        if key in emitted:
            continue
        ev = dict(correlated)
        ev["ts"] = datetime.now().isoformat(timespec="seconds")
        ev["citofono_hits_count"] = face.get("hits_count")
        emit(ev)
        apply_event_to_registry(ev)
        emitted.add(key)
        print(json.dumps(ev, ensure_ascii=False))

    state["emitted"] = sorted(emitted)[-5000:]
    save_state(state)

if __name__ == "__main__":
    main()
