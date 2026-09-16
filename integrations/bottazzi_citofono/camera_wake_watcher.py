import os
import time
import json
import subprocess
import tempfile
import shutil
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
try:
    from .face_consensus import summarize_face_burst
except ImportError:
    from face_consensus import summarize_face_burst
try:
    import cv2
except Exception:
    cv2 = None

load_dotenv(os.path.expanduser("~/.secrets/homeassistant.env"))

HA_TOKEN = os.environ["HA_TOKEN"]
BOTTAZZI_TOKEN = os.environ["BOTTAZZI_TOKEN"]

HA_URL = "http://127.0.0.1:8123"
BRIDGE_URL = "http://127.0.0.1:18123"
FACE_API_URL = "http://127.0.0.1:18124"
ENTITY_ID = "camera.citofono"
RTSP_URL = "rtsp://127.0.0.1:8555/citofono_tuya?media=video+audio"
RTSP_VIDEO_URL = "rtsp://127.0.0.1:8555/citofono_tuya?media=video"
EVENTS_DIR = Path("/opt/bottazzi-citofono/events")
EVENTS_JSONL = Path("/opt/bottazzi-citofono/events.jsonl")
WAKE_EVENTS_JSONL = Path("/opt/bottazzi-citofono/wake_events.jsonl")
VOICE_QUEUE_DIR = Path("/run/bottazzi/voice_chunks")

AUDIO_FILE = Path("/run/bottazzi/audio_armed_until")
GATE_FILE = Path("/run/bottazzi/gate_armed_until")
CITOFONO_WAKE_FILE = Path("/run/bottazzi/citofono_wake_until")
CITOFONO_DOOR_MOTION_FILE = Path("/run/bottazzi/citofono_door_motion_until")
FACE_MAX_BEST_DISTANCE = 0.39
FACE_ARM_ALLOWLIST = {x.strip().lower() for x in os.environ.get("BOTTAZZI_GATE_FACE_ALLOWLIST", "fabio").split(",") if x.strip()}
VOICE_FILE = Path("/run/bottazzi/voice_open_until")
FACE_FILE = Path("/run/bottazzi/face_seen_until")
AUDIO_SECONDS = 90
GATE_SECONDS = 180
CHECK_SECONDS = 1
COOLDOWN_SECONDS = 8
WAKE_AUDIO_SECONDS = 7
DOOR_MOTION_ROI = os.environ.get("BOTTAZZI_CITOFONO_DOOR_ROI", "0,0,1,1")
DOOR_MOTION_MIN_RATIO = float(os.environ.get("BOTTAZZI_CITOFONO_DOOR_MOTION_MIN_RATIO", "0.012"))
DOOR_MOTION_MIN_SCORE = float(os.environ.get("BOTTAZZI_CITOFONO_DOOR_MOTION_MIN_SCORE", "8.0"))
FACE_BURST_FPS = float(os.environ.get("BOTTAZZI_CITOFONO_FACE_BURST_FPS", "2.0"))
FACE_BURST_FRAMES = int(os.environ.get("BOTTAZZI_CITOFONO_FACE_BURST_FRAMES", "10"))
FACE_BURST_WAKE_DELAY = float(os.environ.get("BOTTAZZI_CITOFONO_FACE_WAKE_DELAY", "0.8"))

last_seen = None
last_trigger = 0.0

def log(obj):
    obj["ts"] = datetime.now().isoformat(timespec="seconds")
    print(json.dumps(obj, ensure_ascii=False), flush=True)

def record_event(event):
    EVENTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def record_wake_event(event):
    WAKE_EVENTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(WAKE_EVENTS_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def capture_clip(dst: Path, seconds: int = 12) -> bool:
    cmd = [
        "timeout", str(seconds + 15),
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-rtsp_transport", "tcp",
        "-i", RTSP_VIDEO_URL,
        "-t", str(seconds),
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(dst),
    ]
    try:
        cp = subprocess.run(cmd, check=False, timeout=seconds + 20, stdin=subprocess.DEVNULL)
        return cp.returncode == 0 and dst.exists() and dst.stat().st_size > 1000
    except Exception as e:
        log({"event": "camera_wake_clip_error", "error": repr(e)})
        return False

def start_clip(dst: Path, seconds: int = 18):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-rtsp_transport", "tcp",
        "-i", RTSP_VIDEO_URL,
        "-t", str(seconds),
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(dst),
    ]
    try:
        return subprocess.Popen(cmd, stdin=subprocess.DEVNULL)
    except Exception as e:
        log({"event": "camera_wake_clip_start_error", "error": repr(e)})
        return None

def finish_clip(proc, dst: Path, timeout: int = 25) -> bool:
    if proc is None:
        return False
    try:
        proc.wait(timeout=timeout)
        return proc.returncode == 0 and dst.exists() and dst.stat().st_size > 1000
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        return dst.exists() and dst.stat().st_size > 1000

def get_camera_last_updated():
    r = requests.get(
        f"{HA_URL}/api/states/{ENTITY_ID}",
        headers={"Authorization": f"Bearer {HA_TOKEN}"},
        timeout=5,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("last_updated"), data.get("state")

def wake_go2rtc_stream():
    try:
        subprocess.run(
            [
                "timeout", "12",
                "curl", "-sS",
                "http://127.0.0.1:1984/api/stream.mjpeg?src=citofono_tuya",
                "-o", "/dev/null",
            ],
            check=False,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        log({"event": "camera_wake_go2rtc_wake_done"})
    except Exception as e:
        log({"event": "camera_wake_go2rtc_wake_error", "error": repr(e)})

def arm_audio(reason):
    until = time.time() + AUDIO_SECONDS
    AUDIO_FILE.parent.mkdir(parents=True, exist_ok=True)
    AUDIO_FILE.write_text(str(until))
    log({
        "event": "camera_wake_audio_armed",
        "reason": reason,
        "audio_seconds": AUDIO_SECONDS,
        "audio_until": until,
    })

def arm_citofono_wake(reason, seconds: int = 240, payload: dict | None = None):
    until = time.time() + seconds
    CITOFONO_WAKE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CITOFONO_WAKE_FILE.write_text(str(until))
    event = {
        "event": "citofono_wake_marker",
        "reason": reason,
        "wake_seconds": seconds,
        "wake_until": until,
    }
    if payload:
        event.update(payload)
    log(event)
    record_wake_event(dict(event, ts=datetime.now().isoformat(timespec="seconds")))

def arm_door_motion(reason, seconds: int = 240):
    until = time.time() + seconds
    CITOFONO_DOOR_MOTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    CITOFONO_DOOR_MOTION_FILE.write_text(str(until))
    log({
        "event": "citofono_door_motion_marker",
        "reason": reason,
        "wake_seconds": seconds,
        "wake_until": until,
    })

def parse_roi(value: str):
    try:
        vals = [float(x.strip()) for x in value.split(",")]
        if len(vals) != 4:
            raise ValueError("roi needs 4 values")
        x1, y1, x2, y2 = vals
        x1, y1 = max(0.0, min(1.0, x1)), max(0.0, min(1.0, y1))
        x2, y2 = max(0.0, min(1.0, x2)), max(0.0, min(1.0, y2))
        if x2 <= x1 or y2 <= y1:
            raise ValueError("invalid roi bounds")
        return x1, y1, x2, y2
    except Exception:
        return 0.0, 0.0, 1.0, 1.0

def detect_door_motion(frames):
    if cv2 is None:
        return {"available": False, "moved": False, "reason": "cv2_unavailable"}
    imgs = []
    for frame in frames:
        img = cv2.imread(str(frame))
        if img is None:
            continue
        imgs.append(img)
    if len(imgs) < 2:
        return {"available": True, "moved": False, "reason": "not_enough_frames", "frames": len(imgs)}

    h, w = imgs[0].shape[:2]
    x1, y1, x2, y2 = parse_roi(DOOR_MOTION_ROI)
    rx1, ry1, rx2, ry2 = int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)
    prev = None
    best = {"motion_ratio": 0.0, "motion_score": 0.0}
    pairs = 0

    for img in imgs:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        roi = gray[ry1:ry2, rx1:rx2]
        if roi.size == 0:
            continue
        roi = cv2.GaussianBlur(roi, (5, 5), 0)
        if prev is not None:
            diff = cv2.absdiff(prev, roi)
            score = float(diff.mean())
            _, mask = cv2.threshold(diff, 24, 255, cv2.THRESH_BINARY)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, None, iterations=1)
            ratio = float(cv2.countNonZero(mask)) / float(max(mask.size, 1))
            if ratio > best["motion_ratio"] or score > best["motion_score"]:
                best = {"motion_ratio": ratio, "motion_score": score}
            pairs += 1
        prev = roi

    moved = best["motion_ratio"] >= DOOR_MOTION_MIN_RATIO and best["motion_score"] >= DOOR_MOTION_MIN_SCORE
    return {
        "available": True,
        "moved": moved,
        "reason": "door_motion_detected" if moved else "door_static_or_low_motion",
        "roi": [x1, y1, x2, y2],
        "pairs": pairs,
        "motion_ratio": round(best["motion_ratio"], 5),
        "motion_score": round(best["motion_score"], 3),
        "min_ratio": DOOR_MOTION_MIN_RATIO,
        "min_score": DOOR_MOTION_MIN_SCORE,
    }

def capture_wake_audio(reason):
    VOICE_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    out = VOICE_QUEUE_DIR / f"wake_{int(time.time())}.wav"
    cmd = [
        "timeout", str(WAKE_AUDIO_SECONDS + 8),
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-y",
        "-rtsp_transport", "tcp",
        "-i", RTSP_URL,
        "-map", "0:a:0",
        "-t", str(WAKE_AUDIO_SECONDS),
        "-vn",
        "-af", "afftdn=nf=-25,volume=30dB,highpass=f=250,lowpass=f=3400",
        "-acodec", "pcm_s16le",
        "-ac", "1",
        "-ar", "16000",
        str(out),
    ]
    try:
        cp = subprocess.run(cmd, check=False, timeout=WAKE_AUDIO_SECONDS + 12, stdin=subprocess.DEVNULL)
        ok = cp.returncode == 0 and out.exists() and out.stat().st_size > 1000
        log({"event": "camera_wake_audio_chunk", "reason": reason, "ok": ok, "wav_path": str(out) if ok else None})
        if not ok:
            try:
                out.unlink()
            except FileNotFoundError:
                pass
        return ok
    except Exception as e:
        log({"event": "camera_wake_audio_chunk_error", "reason": reason, "error": repr(e)})
        return False

def arm_gate(reason, summary):
    until = time.time() + GATE_SECONDS
    GATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    GATE_FILE.write_text(str(until))
    FACE_FILE.write_text(str(until))
    log({
        "event": "camera_wake_gate_armed",
        "reason": reason,
        "gate_seconds": GATE_SECONDS,
        "gate_until": until,
        "summary": summary,
    })

def has_voice_consent():
    try:
        until = float(VOICE_FILE.read_text().strip())
        return until > time.time(), until - time.time()
    except Exception:
        return False, 0.0

def try_open_after_face():
    voice_ok, voice_left = has_voice_consent()
    if not voice_ok:
        return

    try:
        import sys
        sys.path.insert(0, "/opt/bottazzi-voice")
        import voice_listener
        log({
            "event": "camera_wake_voice_preexisting",
            "voice_seconds_left": round(voice_left, 1),
        })
        voice_listener.open_gate("face_after_preexisting_voice")
    except Exception as e:
        log({"event": "camera_wake_open_after_face_error", "error": repr(e)})

def trigger_face_burst():
    hits = []
    errors = []
    frames_checked = 0
    event_id = f"citofono_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time())}"
    event_dir = EVENTS_DIR / event_id
    event_dir.mkdir(parents=True, exist_ok=True)
    clip_path = event_dir / "clip.mp4"
    clip_proc = start_clip(clip_path)

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        pattern = td_path / "frame_%03d.jpg"

        frames = []
        for attempt in range(1, 5):
            wake_go2rtc_stream()
            time.sleep(FACE_BURST_WAKE_DELAY)
            subprocess.run(
                [
                    "timeout", "18",
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-rtsp_transport", "tcp",
                    "-analyzeduration", "1000000",
                    "-probesize", "1000000",
                    "-i", RTSP_VIDEO_URL,
                    "-vf", f"fps={FACE_BURST_FPS:g}",
                    "-frames:v", str(FACE_BURST_FRAMES),
                    str(pattern),
                ],
                check=False,
                timeout=22,
                stdin=subprocess.DEVNULL,
            )
            frames = sorted(td_path.glob("frame_*.jpg"))
            if frames:
                log({"event": "camera_wake_frames_ready", "attempt": attempt, "frames": len(frames)})
                break
            log({"event": "camera_wake_frames_empty_retry", "attempt": attempt})

        event_frames = []
        for frame in frames:
            dst = event_dir / frame.name
            shutil.copy2(frame, dst)
            event_frames.append(dst)

        for frame in event_frames:
            try:
                r = requests.post(
                    f"{FACE_API_URL}/recognize_image_path",
                    headers={"X-Bottazzi-Token": BOTTAZZI_TOKEN},
                    json={"path": str(frame)},
                    timeout=35,
                )
                frames_checked += 1

                if not r.ok:
                    errors.append({"frame": frame.name, "status_code": r.status_code, "text": r.text[-500:]})
                    continue

                data = r.json()
                for item in data.get("results", []):
                    m = item.get("match")
                    if m and m != "sconosciuto" and not m.startswith("forse_"):
                        hits.append({
                            "frame": frame.name,
                            **item,
                        })
            except Exception as e:
                errors.append({"frame": frame.name, "error": repr(e)})

    consensus = summarize_face_burst(
        hits,
        frames_checked=frames_checked,
        allowlist=FACE_ARM_ALLOWLIST,
    )
    summary = consensus["summary"]
    verdict = consensus["verdict"]

    log({
        "event": "camera_wake_face_burst",
        "event_id": event_id,
        "event_dir": str(event_dir),
        "verdict": verdict,
        "summary": summary,
        "hits_count": len(hits),
        "errors_count": len(errors),
        "frames_checked": frames_checked,
        "consensus": consensus,
    })

    clip_ok = finish_clip(clip_proc, clip_path)
    door_motion = detect_door_motion(event_frames)
    if door_motion.get("moved"):
        arm_door_motion("citofono_door_motion_detected")
    event_ts = datetime.now().isoformat(timespec="seconds")
    event_data = {
        "ts": event_ts,
        "event_id": event_id,
        "source": "citofono",
        "event": "camera_wake_face_burst",
        "verdict": verdict,
        "summary": summary,
        "hits_count": len(hits),
        "errors_count": len(errors),
        "frames_checked": frames_checked,
        "consensus": consensus,
        "event_dir": str(event_dir),
        "frames": [str(p) for p in sorted(event_dir.glob("frame_*.jpg"))],
        "clip": str(clip_path) if clip_ok else None,
        "clip_ok": clip_ok,
        "door_motion": door_motion,
    }
    (event_dir / "event.json").write_text(json.dumps(event_data, ensure_ascii=False, indent=2), encoding="utf-8")
    record_event(event_data)

    if consensus.get("authorized"):
        arm_gate("camera_wake_face_burst_multiframe_consensus", summary)
        try_open_after_face()
    elif verdict not in ("nessun_volto_riconosciuto", "incerto"):
        log({"event": "camera_wake_face_not_authorized", "verdict": verdict, "reason": consensus.get("reason")})

def main():
    global last_seen, last_trigger

    log({
        "event": "camera_wake_watcher_started",
        "entity_id": ENTITY_ID,
        "check_seconds": CHECK_SECONDS,
    })

    while True:
        try:
            updated, state = get_camera_last_updated()

            if last_seen is None:
                last_seen = updated
                log({
                    "event": "camera_wake_baseline",
                    "state": state,
                    "last_updated": updated,
                })

            elif updated and updated != last_seen:
                now = time.time()
                old = last_seen
                last_seen = updated

                log({
                    "event": "camera_wake_detected",
                    "state": state,
                    "old_last_updated": old,
                    "new_last_updated": updated,
                })

                if state == "idle":
                    log({"event": "camera_wake_ignored_idle_heartbeat", "new_last_updated": updated})
                    time.sleep(CHECK_SECONDS)
                    continue

                if now - last_trigger >= COOLDOWN_SECONDS:
                    last_trigger = now
                    arm_citofono_wake(
                        "camera_last_updated_changed",
                        payload={
                            "state": state,
                            "old_last_updated": old,
                            "new_last_updated": updated,
                        },
                    )
                    wake_go2rtc_stream()
                    arm_audio("camera_last_updated_changed")
                    capture_wake_audio("camera_last_updated_changed")
                    time.sleep(1)
                    trigger_face_burst()
                else:
                    log({"event": "camera_wake_ignored_cooldown"})

        except Exception as e:
            log({"event": "camera_wake_error", "error": repr(e)})

        time.sleep(CHECK_SECONDS)

if __name__ == "__main__":
    main()
