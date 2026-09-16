import os
import time
import json
import base64
import subprocess
import threading
from datetime import datetime
from pathlib import Path

import cv2
import requests
from garden_model_contract import load_model_spec, verify_local_model
from person_event_supervisor import load_config as load_supervisor_config, PersonEventSupervisor, analyze_frame_health
try:
    from ultralytics import YOLO
except Exception as exc:
    YOLO = None
    YOLO_IMPORT_ERROR = repr(exc)
else:
    YOLO_IMPORT_ERROR = None

CAMERA_STREAM = "http://127.0.0.1:1984/api/frame.jpeg?src=camera_giardino_anteriore"
CAMERA_RTSP_VIDEO = "rtsp://127.0.0.1:8555/camera_giardino_anteriore"
FRAME_CACHE_FILE = os.environ.get("BOTTAZZI_GARDEN_FRAME_CACHE_FILE", "").strip()
FRAME_CACHE_MAX_AGE_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_FRAME_CACHE_MAX_AGE_SECONDS", "15"))
MEOWGRAM_URL = "http://127.0.0.1:18127/send_photo"
MEOWGRAM_FILE_URL = "http://127.0.0.1:18127/send_file"
VISION_BASE_URL = os.environ.get("BOTTAZZI_GARDEN_VISION_BASE_URL", "http://127.0.0.1:19112").rstrip("/")
VISION_MODEL = os.environ.get("BOTTAZZI_GARDEN_VISION_MODEL", "gemma4-vision")
BASE = Path(os.environ.get("BOTTAZZI_GARDEN_BASE", "/opt/bottazzi-garden")).resolve()
YOLO_MODEL_MANIFEST = Path(
    os.environ.get("BOTTAZZI_GARDEN_YOLO_MANIFEST", str(BASE / "garden_model_manifest.json"))
)
YOLO_MODEL_SPEC = load_model_spec(YOLO_MODEL_MANIFEST)

# GARDEN_GLITCH_FAILSOFT_RETRY_PATCH_20260620
# GARDEN_GLITCH_FAILSOFT_RETRY_FIX_20260620
# GARDEN_YOLO_PRIMARY_GEMMA_GATE_PATCH_20260620
# GARDEN_GATE_SMALL_MOTION_REVIEW_PATCH_20260620
GLITCH_FAILSOFT_ENABLED = os.environ.get("BOTTAZZI_GARDEN_GLITCH_FAILSOFT", "1") != "0"
GLITCH_FAILSOFT_WINDOW_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_GLITCH_FAILSOFT_WINDOW_SECONDS", "120"))
GLITCH_FAILSOFT_RETRY_ENABLED = os.environ.get("BOTTAZZI_GARDEN_GLITCH_FAILSOFT_RETRY", "1") != "0"
GLITCH_FAILSOFT_REVIEW_COOLDOWN_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_GLITCH_FAILSOFT_REVIEW_COOLDOWN_SECONDS", "45"))
GATE_SMALL_MOTION_REVIEW_ENABLED = os.environ.get("BOTTAZZI_GARDEN_GATE_SMALL_MOTION_REVIEW", "1") != "0"
GATE_SMALL_MOTION_REVIEW_COOLDOWN_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_GATE_SMALL_MOTION_REVIEW_COOLDOWN_SECONDS", "90"))
CITOFONO_GATE_CAPTURE_ENABLED = os.environ.get("BOTTAZZI_GARDEN_CITOFONO_GATE_CAPTURE", "1") != "0"
CITOFONO_GATE_CAPTURE_COUNT = int(os.environ.get("BOTTAZZI_GARDEN_CITOFONO_GATE_CAPTURE_COUNT", "4"))
CITOFONO_GATE_CAPTURE_DELAY_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_CITOFONO_GATE_CAPTURE_DELAY_SECONDS", "1.0"))
YOLO_ENABLED = os.environ.get("BOTTAZZI_GARDEN_USE_YOLO", "1") != "0"
YOLO_MODEL_PATH = os.environ.get("BOTTAZZI_GARDEN_YOLO_MODEL", str(YOLO_MODEL_SPEC.path))
YOLO_MODEL_SHA256 = os.environ.get("BOTTAZZI_GARDEN_YOLO_MODEL_SHA256", YOLO_MODEL_SPEC.sha256)
YOLO_MODEL_ID = YOLO_MODEL_SPEC.model_id
YOLO_MODEL_REVISION = YOLO_MODEL_SPEC.revision
YOLO_DETECTOR_NAME = YOLO_MODEL_SPEC.detector_name
YOLO_CONFIDENCE = float(os.environ.get("BOTTAZZI_GARDEN_YOLO_CONFIDENCE", "0.22"))
YOLO_IMGSZ = int(os.environ.get("BOTTAZZI_GARDEN_YOLO_IMGSZ", "640"))
YOLO_REMOTE_URL = os.environ.get(
    "BOTTAZZI_GARDEN_YOLO_REMOTE_URL",
    "http://10.252.14.12:18129/detect",
).strip()
YOLO_REMOTE_TIMEOUT_SECONDS = float(
    os.environ.get("BOTTAZZI_GARDEN_YOLO_REMOTE_TIMEOUT_SECONDS", "5")
)
VISION_VALIDATE_YOLO = os.environ.get("BOTTAZZI_GARDEN_VALIDATE_YOLO", "1") != "0"
VISION_VALIDATE_DNN_NIGHT = os.environ.get("BOTTAZZI_GARDEN_VALIDATE_DNN_NIGHT", "1") != "0"
VISION_VALIDATE_DNN_BORDERLINE = os.environ.get("BOTTAZZI_GARDEN_VALIDATE_DNN_BORDERLINE", "1") != "0"
VISION_VALIDATE_MOTION = os.environ.get("BOTTAZZI_GARDEN_VALIDATE_MOTION", "1") == "1"
VISION_BORDERLINE_CONFIDENCE = float(os.environ.get("BOTTAZZI_GARDEN_VISION_BORDERLINE_CONFIDENCE", "0.78"))
CHAT_ID = os.environ.get("BOTTAZZI_GARDEN_CHAT_ID", "7303247209")

SNAP_DIR = BASE / "snapshots"
EVENTS_DIR = BASE / "events"
EVENTS_JSONL = BASE / "events.jsonl"
REVIEW_QUEUE_JSONL = BASE / "review_queue.jsonl"
SUPERVISOR_CONFIG = BASE / "event_supervisor_config.json"
PERSON_EVENTS_JSONL = BASE / "logs" / "person_events.jsonl"
CITOFONO_EVENTS_JSONL = Path("/opt/bottazzi-citofono/events.jsonl")
CITOFONO_WAKE_EVENTS_JSONL = Path("/opt/bottazzi-citofono/wake_events.jsonl")
CITOFONO_EVENTS_DIR = Path("/opt/bottazzi-citofono/events")
CITOFONO_WAKE_FILE = Path("/run/bottazzi/citofono_wake_until")
CITOFONO_DOOR_MOTION_FILE = Path("/run/bottazzi/citofono_door_motion_until")
CITOFONO_MEDIA_SUFFIXES = {".jpg", ".jpeg", ".png", ".mp4", ".mov", ".avi", ".mkv", ".webm"}
SNAP_DIR.mkdir(parents=True, exist_ok=True)
EVENTS_DIR.mkdir(parents=True, exist_ok=True)

HA_TOKEN = os.environ.get("HA_TOKEN")
if not HA_TOKEN:
    env_path = Path.home() / ".secrets/homeassistant.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip().startswith("HA_TOKEN="):
                HA_TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")

if not HA_TOKEN:
    raise RuntimeError("HA_TOKEN mancante")

CHECK_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_CHECK_SECONDS", "3"))
FRAME_TIMEOUT_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_FRAME_TIMEOUT_SECONDS", "4"))
MAX_GRAB_ATTEMPTS = int(os.environ.get("BOTTAZZI_GARDEN_MAX_GRAB_ATTEMPTS", "1"))
GRAB_RETRY_SLEEP_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_GRAB_RETRY_SLEEP_SECONDS", "0.4"))
GRAB_FAILURE_BACKOFF_SECONDS = float(os.environ.get("BOTTAZZI_GARDEN_GRAB_FAILURE_BACKOFF_SECONDS", "12"))
STREAM_RECOVERY_FAILURES = 3
STREAM_RECOVERY_COOLDOWN_SECONDS = 180.0
STREAM_RECOVERY_OWNER = os.getenv("BOTTAZZI_GARDEN_STREAM_RECOVERY_OWNER", "detector").strip().lower()
COOLDOWN_SECONDS = 60
MIN_CONFIDENCE_WEIGHT = 0.65
MIN_PERSON_HEIGHT = 85
MIN_PERSON_WIDTH = 40
DNN_CONFIDENCE_DAY = 0.45
DNN_CONFIDENCE_NIGHT = 0.65
SMALL_BOX_CONFIDENCE = 0.60
MIN_PERSON_CENTER_Y_RATIO = 0.16
EDGE_MARGIN_RATIO = 0.035
IGNORE_ZONES = [
    # Patio furniture on the right side is repeatedly detected as a person.
    (0.74, 0.20, 1.00, 0.78),
]
IGNORE_OVERLAP_RATIO = 0.35
NIGHT_START_HOUR = 20
NIGHT_END_HOUR = 7
MAX_BOX_AREA_RATIO = 0.55
DNN_PROTOTXT = "/opt/bottazzi-garden/models/MobileNetSSD_deploy.prototxt"
DNN_MODEL = "/opt/bottazzi-garden/models/MobileNetSSD_deploy.caffemodel"
DNN_CLASSES = [
    "background", "aeroplane", "bicycle", "bird", "boat",
    "bottle", "bus", "car", "cat", "chair", "cow", "diningtable",
    "dog", "horse", "motorbike", "person", "pottedplant",
    "sheep", "sofa", "train", "tvmonitor"
]

net = cv2.dnn.readNetFromCaffe(DNN_PROTOTXT, DNN_MODEL)
yolo_model = None
yolo_load_error = None

last_alert = 0.0
last_review_alert = 0.0
last_gate_small_motion_review = 0.0
last_motion_frame = None
recent_motion_fallback_alert_candidates = []
prev_supervisor_frame = None
prev_supervisor_ts = None
supervisor_freeze_state = {}
supervisor_frame_seq = 0
supervisor_cfg = load_supervisor_config(SUPERVISOR_CONFIG)
person_supervisor = PersonEventSupervisor(supervisor_cfg, PERSON_EVENTS_JSONL)

def log(obj):
    obj["ts"] = datetime.now().isoformat(timespec="seconds")
    print(json.dumps(obj, ensure_ascii=False), flush=True)

def bbox_observability_metrics(candidate, frame_shape=None):
    box = candidate.get("box") or [0, 0, 0, 0]
    x, y, bw, bh = [float(v) for v in (box + [0, 0, 0, 0])[:4]]
    frame_h = float(frame_shape[0]) if frame_shape is not None and len(frame_shape) >= 2 else 0.0
    frame_w = float(frame_shape[1]) if frame_shape is not None and len(frame_shape) >= 2 else 0.0
    area_ratio = candidate.get("area_ratio")
    if area_ratio is None and frame_w > 0 and frame_h > 0:
        area_ratio = (max(bw, 0.0) * max(bh, 0.0)) / max(frame_w * frame_h, 1.0)
    aspect_ratio_h_over_w = bh / max(bw, 1.0)
    center = {"x": round(x + bw / 2.0, 2), "y": round(y + bh / 2.0, 2)}
    if frame_w > 0 and frame_h > 0:
        center["x_norm"] = round(center["x"] / frame_w, 4)
        center["y_norm"] = round(center["y"] / frame_h, 4)
    return {
        "bbox": [round(x, 2), round(y, 2), round(bw, 2), round(bh, 2)],
        "area_ratio": round(float(area_ratio or 0.0), 4),
        "aspect_ratio_h_over_w": round(aspect_ratio_h_over_w, 4),
        "center": center,
    }

def enrich_person_candidate(candidate, frame_shape=None):
    enriched = dict(candidate)
    enriched.update(bbox_observability_metrics(enriched, frame_shape))
    enriched["source"] = "garden_detector"
    return enriched

def foliage_leaf_false_positive_shadow_gate(candidate, frame_shape=None, motion_score=None):
    metrics = bbox_observability_metrics(candidate, frame_shape)
    area = float(metrics.get("area_ratio") or 0.0)
    aspect = float(metrics.get("aspect_ratio_h_over_w") or 0.0)
    box = metrics.get("bbox") or [0, 0, 0, 0]
    bw = float(box[2]) if len(box) >= 4 else 0.0
    bh = float(box[3]) if len(box) >= 4 else 0.0
    reasons = []
    if area <= 0 or bw <= 0 or bh <= 0:
        return {"shadow_hit": False, "reason": "insufficient_metrics", **metrics}
    if area < 0.008:
        reasons.append("tiny_area")
    if aspect >= 4.2 or aspect <= 0.28:
        reasons.append("non_person_aspect")
    if bw < 25 or bh < 35:
        reasons.append("thin_or_small_bbox")
    if motion_score is not None and float(motion_score or 0.0) < 0.015 and area < 0.04:
        reasons.append("very_low_motion_small_box")
    return {"shadow_hit": bool(reasons), "reason": "+".join(reasons) if reasons else "metrics_not_foliage_like", **metrics}

def log_person_candidate_event(event_name, candidate, frame_shape=None, image=None, reason=None, motion_score=None):
    enriched = enrich_person_candidate(candidate, frame_shape)
    if reason is not None:
        enriched["reason"] = reason
    if motion_score is not None:
        enriched["motion_score"] = round(float(motion_score or 0.0), 4)
    payload = {"event": event_name, "candidate": enriched}
    if image is not None:
        payload["image"] = str(image)
    log(payload)

def furniture_zone_false_positive_gate(candidate):
    metrics = bbox_observability_metrics(candidate)
    candidate_center = candidate.get("center") or {}
    metric_center = metrics.get("center") or {}
    x_norm = candidate_center.get("x_norm", metric_center.get("x_norm"))
    y_norm = candidate_center.get("y_norm", metric_center.get("y_norm"))
    if x_norm is None or y_norm is None:
        x_norm = 0.0
        y_norm = 1.0
    x_norm = float(x_norm)
    y_norm = float(y_norm)
    if "x_norm" not in metric_center and x_norm:
        metric_center["x_norm"] = round(x_norm, 4)
    if "y_norm" not in metric_center and y_norm != 1.0:
        metric_center["y_norm"] = round(y_norm, 4)
    metrics["center"] = metric_center
    area = float(metrics.get("area_ratio") or 0.0)
    overlap = float(candidate.get("ignored_zone_overlap") or 0.0)
    motion = float(candidate.get("supervisor_motion_score") or 0.0)
    reason = str(candidate.get("supervisor_reason") or "")
    zone_override = str(candidate.get("zone_override") or "")

    furniture_zone = zone_override == "possible_person_in_furniture_zone" or overlap >= 0.45
    right_high_zone = x_norm >= 0.75 and y_norm <= 0.55
    recurrent_area = 0.12 <= area <= 0.32
    fast_low_motion = reason == "fast_person_detected" and motion < 0.07

    gate_hit = furniture_zone and right_high_zone and recurrent_area and fast_low_motion
    return {
        "gate_hit": gate_hit,
        "reason": "furniture_zone_fast_low_motion" if gate_hit else "conditions_not_met",
        "ignored_zone_overlap": round(overlap, 3),
        "zone_override": zone_override,
        "supervisor_reason": reason,
        "supervisor_motion_score": round(motion, 4),
        **metrics,
    }

def filter_furniture_zone_false_positive_hits(hits, image=None):
    kept = []
    rejected = []
    for h in hits:
        gate = furniture_zone_false_positive_gate(h)
        if gate.get("gate_hit"):
            hh = enrich_person_candidate(h)
            hh.update({
                "reason": "furniture_zone_fast_low_motion",
                "furniture_leaf_gate": gate,
            })
            rejected.append(hh)
        else:
            kept.append(h)
    if rejected:
        payload = {
            "event": "person_candidate_rejected_furniture_leaf_gate",
            "count": len(rejected),
            "rejected": rejected[:10],
            "source": "garden_detector",
        }
        if image is not None:
            payload["image"] = str(image)
        log(payload)
    return kept

def append_jsonl(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

def queue_rejected_review(img_path: Path, rejected, source: str):
    candidates = []
    for item in rejected:
        area_ratio = float(item.get("area_ratio") or 0)
        confidence = float(item.get("confidence") or 0)
        reason = str(item.get("reason") or "")
        if any(token in reason for token in ("edge", "ignored_zone", "too_large", "smear", "artifact")):
            continue
        if source == "motion_rejected":
            box = item.get("box") or [0, 0, 0, 0]
            width = float(box[2]) if len(box) >= 4 else 0
            height = float(box[3]) if len(box) >= 4 else 0
            if height < 90 or width < 35 or area_ratio < 0.02:
                continue
        if area_ratio >= 0.02 or confidence >= 0.55:
            candidates.append(item)
    if not candidates:
        return
    append_jsonl(REVIEW_QUEUE_JSONL, {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": source,
        "image": str(img_path),
        "candidates": candidates[:12],
        "status": "pending",
    })

def is_gate_small_motion_candidate(item) -> bool:
    """Small left/gate motion is not enough for alert, but must not disappear."""
    if str(item.get("detector") or "") != "motion_fallback":
        return False
    reason = str(item.get("reason") or "")
    if reason != "motion_too_small":
        return False
    box = item.get("box") or [0, 0, 0, 0]
    if len(box) < 4:
        return False
    try:
        x, y, width, height = [float(v) for v in box[:4]]
        area_ratio = float(item.get("area_ratio") or 0.0)
        ignored = float(item.get("ignored_zone_overlap") or 0.0)
    except Exception:
        return False

    center_x = x + width / 2.0
    center_y = y + height / 2.0
    gate_left_zone = center_x <= 125 and center_y >= 135
    passage_sized = width >= 30 and height >= 90 and 0.018 <= area_ratio <= 0.08
    return gate_left_zone and passage_sized and ignored < 0.10

def capture_citofono_gate_review_async(trigger_image: Path, candidates, reason: str):
    if not CITOFONO_GATE_CAPTURE_ENABLED:
        return False

    def _worker():
        event_id = f"citofono_gate_from_garden_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time())}"
        event_dir = CITOFONO_EVENTS_DIR / event_id
        event_dir.mkdir(parents=True, exist_ok=True)
        frames = []
        errors = []
        count = max(1, min(CITOFONO_GATE_CAPTURE_COUNT, 8))
        delay = max(0.2, min(CITOFONO_GATE_CAPTURE_DELAY_SECONDS, 3.0))
        for idx in range(count):
            frame_path = event_dir / f"frame_{idx + 1:03d}.jpg"
            try:
                r = requests.get(
                    "http://127.0.0.1:1984/api/frame.jpeg",
                    params={"src": "citofono_tuya"},
                    timeout=8,
                )
                if not r.ok or len(r.content or b"") < 5000:
                    errors.append({
                        "frame": idx + 1,
                        "status_code": r.status_code,
                        "bytes": len(r.content or b""),
                        "text": r.text[:300] if r.text else "",
                    })
                else:
                    frame_path.write_bytes(r.content)
                    frames.append(str(frame_path))
            except Exception as exc:
                errors.append({"frame": idx + 1, "error": repr(exc)})
            time.sleep(delay)

        event_data = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event_id": event_id,
            "source": "citofono",
            "event": "garden_gate_small_motion_citofono_capture",
            "reason": reason,
            "trigger_image": str(trigger_image),
            "garden_candidates": candidates[:8],
            "event_dir": str(event_dir),
            "frames": frames,
            "frames_checked": len(frames),
            "errors": errors,
        }
        try:
            (event_dir / "event.json").write_text(json.dumps(event_data, ensure_ascii=False, indent=2), encoding="utf-8")
            append_jsonl(CITOFONO_EVENTS_JSONL, event_data)
        except Exception as exc:
            log({"event": "citofono_gate_capture_record_failed", "error": repr(exc), "event_id": event_id})
        log({
            "event": "citofono_gate_capture_done",
            "event_id": event_id,
            "frames_count": len(frames),
            "errors_count": len(errors),
            "trigger_image": str(trigger_image),
        })
        if frames:
            try:
                with open(frames[0], "rb") as fh:
                    resp = requests.post(
                        MEOWGRAM_URL,
                        data={
                            "chat_id": CHAT_ID,
                            "caption": (
                                "🚪 Citofono catturato da movimento cancello giardino\n"
                                f"reason: {reason}\n"
                                f"event_id: {event_id}\n"
                                f"Ora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                            ),
                        },
                        files={"photo": fh},
                        timeout=30,
                    )
                log({"event": "telegram_sent", "kind": "citofono_gate_capture", "status": resp.status_code, "text": resp.text[:500], "event_id": event_id})
            except Exception as exc:
                log({"event": "citofono_gate_capture_send_failed", "error": repr(exc), "event_id": event_id})

    threading.Thread(target=_worker, daemon=True).start()
    return True

def maybe_send_gate_small_motion_review(img_path: Path, rejected) -> bool:
    global last_gate_small_motion_review
    if not GATE_SMALL_MOTION_REVIEW_ENABLED:
        return False
    candidates = [dict(item) for item in rejected if is_gate_small_motion_candidate(item)]
    if not candidates:
        return False
    now = time.time()
    if now - last_gate_small_motion_review < GATE_SMALL_MOTION_REVIEW_COOLDOWN_SECONDS:
        log({
            "event": "gate_small_motion_review_suppressed_cooldown",
            "cooldown_seconds": GATE_SMALL_MOTION_REVIEW_COOLDOWN_SECONDS,
            "image": str(img_path),
            "candidates": candidates[:6],
        })
        return False
    last_gate_small_motion_review = now
    for item in candidates:
        item["review_reason"] = "gate_small_motion_possible_person"
    log({
        "event": "gate_small_motion_review_candidate",
        "image": str(img_path),
        "candidates": candidates[:6],
    })
    capture_citofono_gate_review_async(img_path, candidates, "gate_small_motion_possible_person")
    return send_review_motion_candidate(
        img_path,
        candidates,
        vision={"human": None, "confidence": 0.0, "reason": "not_run_small_gate_motion_review"},
        reason="gate_small_motion_possible_person",
    )

def is_night_now() -> bool:
    hour = datetime.now().hour
    return hour >= NIGHT_START_HOUR or hour < NIGHT_END_HOUR

def current_confidence_threshold() -> float:
    return DNN_CONFIDENCE_NIGHT if is_night_now() else DNN_CONFIDENCE_DAY

def overlap_ratio(box, zone, image_w, image_h):
    x, y, bw, bh = box
    zx1, zy1, zx2, zy2 = zone
    zx1, zy1, zx2, zy2 = zx1 * image_w, zy1 * image_h, zx2 * image_w, zy2 * image_h
    ix1, iy1 = max(x, zx1), max(y, zy1)
    ix2, iy2 = min(x + bw, zx2), min(y + bh, zy2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    return ((ix2 - ix1) * (iy2 - iy1)) / float(max(bw * bh, 1))

def validate_grabbed_frame(dst: Path, source: str, attempt=None):
    if dst.exists() and dst.stat().st_size > 1000:
        img = cv2.imread(str(dst))
        if img is None:
            return False, "decoded_empty_frame"
        if has_vertical_smear_artifact(img):
            reason = "vertical_smear_artifact"
            log({"event": "grab_rejected_corrupt_frame", "attempt": attempt, "reason": reason, "source": source, "image": str(dst)})
            return False, reason
        return True, None
    return False, "empty_frame"

def try_grab_cached_frame(dst: Path):
    if not FRAME_CACHE_FILE:
        return False, "cache_disabled"
    try:
        cache_path = Path(FRAME_CACHE_FILE)
        st = cache_path.stat()
        age = time.time() - st.st_mtime
        if age > FRAME_CACHE_MAX_AGE_SECONDS:
            return False, f"cache_stale:{age:.1f}s"
        if st.st_size <= 1000:
            return False, "cache_empty_frame"
        dst.write_bytes(cache_path.read_bytes())
        ok, error = validate_grabbed_frame(dst, "cache", None)
        return ok, error
    except Exception as e:
        return False, repr(e)

def grab_frame(dst: Path) -> bool:
    cache_ok, cache_error = try_grab_cached_frame(dst)
    if cache_ok:
        return True
    if FRAME_CACHE_FILE:
        log({"event": "grab_cache_miss", "cache": FRAME_CACHE_FILE, "error": cache_error})

    last_error = None
    for attempt in range(1, MAX_GRAB_ATTEMPTS + 1):
        try:
            r = requests.get(CAMERA_STREAM, timeout=FRAME_TIMEOUT_SECONDS)
            r.raise_for_status()
            dst.write_bytes(r.content)
            ok, last_error = validate_grabbed_frame(dst, "http", attempt)
            if ok:
                return True
        except Exception as e:
            last_error = repr(e)
        log({"event": "grab_retry", "attempt": attempt, "error": last_error})
        time.sleep(GRAB_RETRY_SLEEP_SECONDS)
    log({"event": "grab_error", "error": last_error})
    return False

def get_yolo_model():
    global yolo_model, yolo_load_error
    if not YOLO_ENABLED:
        return None
    if yolo_model is not None:
        return yolo_model
    if YOLO is None:
        yolo_load_error = YOLO_IMPORT_ERROR or "ultralytics_import_failed"
        log({"event": "yolo_unavailable", "reason": yolo_load_error})
        return None
    try:
        verified_model = verify_local_model(Path(YOLO_MODEL_PATH), YOLO_MODEL_SHA256)
        yolo_model = YOLO(str(verified_model))
        names = getattr(yolo_model, "names", {}) or {}
        if names.get(0) != "person":
            raise RuntimeError("yolo_person_class_contract_failed")
        log({
            "event": "yolo_loaded",
            "model": str(verified_model),
            "model_id": YOLO_MODEL_ID,
            "revision": YOLO_MODEL_REVISION,
            "sha256": YOLO_MODEL_SHA256,
            "detector": YOLO_DETECTOR_NAME,
            "confidence": YOLO_CONFIDENCE,
            "imgsz": YOLO_IMGSZ,
        })
        return yolo_model
    except Exception as exc:
        yolo_load_error = repr(exc)
        log({"event": "yolo_unavailable", "reason": yolo_load_error, "model": YOLO_MODEL_PATH})
        return None

def detect_people_yolo(img_path: Path, img):
    if not YOLO_ENABLED:
        return None

    h, w = img.shape[:2]
    hits = []
    rejected = []

    # L'inferenza YOLO viene eseguita su Temistocle.
    # Nessun fallback al modello CUDA locale: in caso di guasto remoto
    # restituiamo None e lasciamo lavorare i fallback già esistenti.
    try:
        with open(img_path, "rb") as fh:
            response = requests.post(
                YOLO_REMOTE_URL,
                params={
                    "conf": YOLO_CONFIDENCE,
                    "imgsz": YOLO_IMGSZ,
                },
                files={
                    "file": (
                        img_path.name,
                        fh,
                        "image/jpeg",
                    )
                },
                timeout=(2.0, YOLO_REMOTE_TIMEOUT_SECONDS),
            )

        response.raise_for_status()
        payload = response.json()

        if not payload.get("ok"):
            raise RuntimeError(f"remote_yolo_not_ok:{payload!r}")

        detections = payload.get("detections") or []

    except Exception as exc:
        log({
            "event": "remote_yolo_unavailable",
            "error": repr(exc),
            "image": str(img_path),
            "url": YOLO_REMOTE_URL,
        })
        return None

    for det in detections:
        try:
            cls_id = int(det.get("cls", -1))
            label = str(det.get("label", ""))

            if label != "person" and cls_id != 0:
                continue

            conf = float(det["confidence"])
            x1, y1, x2, y2 = [int(v) for v in det["xyxy"]]
        except Exception:
            continue

        rw, rh = x2 - x1, y2 - y1

        item = {
            "box": [x1, y1, rw, rh],
            "confidence": round(conf, 3),
            "detector": YOLO_DETECTOR_NAME,
            "threshold": YOLO_CONFIDENCE,
            "night_mode": is_night_now(),
        }

        item.update(bbox_observability_metrics(item, img.shape))
        log_person_candidate_event(
            "person_candidate_observed",
            item,
            img.shape,
            img_path,
        )

        center_x = x1 + rw / 2.0
        center_y = y1 + rh / 2.0
        edge_margin = w * EDGE_MARGIN_RATIO
        area_ratio = (
            max(rw, 0) * max(rh, 0)
        ) / float(max(w * h, 1))

        item["area_ratio"] = round(area_ratio, 3)

        if center_y < h * MIN_PERSON_CENTER_Y_RATIO:
            item["reason"] = "top_edge_false_positive"
            rejected.append(item)
            continue

        if center_x < edge_margin or center_x > (w - edge_margin):
            item["reason"] = "side_edge_false_positive"
            rejected.append(item)
            continue

        max_ignored = max(
            (
                overlap_ratio(
                    (x1, y1, rw, rh),
                    z,
                    w,
                    h,
                )
                for z in IGNORE_ZONES
            ),
            default=0.0,
        )

        item["ignored_zone_overlap"] = round(max_ignored, 3)

        if max_ignored >= IGNORE_OVERLAP_RATIO:
            zone_override = (
                conf >= 0.60
                and area_ratio >= 0.045
                and rh >= 95
                and rw >= 55
            )

            if not zone_override:
                item["reason"] = "ignored_furniture_zone"
                rejected.append(item)
                continue

            item["zone_override"] = "possible_person_in_furniture_zone"

        if x1 < 0 or y1 < 0 or x2 > w or y2 > h:
            item["reason"] = "box_out_of_frame"
            rejected.append(item)
            continue

        if area_ratio > MAX_BOX_AREA_RATIO:
            item["reason"] = "box_too_large"
            rejected.append(item)
            continue

        if rh < MIN_PERSON_HEIGHT or rw < MIN_PERSON_WIDTH:
            item["reason"] = "too_small"
            rejected.append(item)
            continue

        hits.append(item)

    if rejected:
        log({
            "event": "yolo_detections_rejected",
            "count": len(rejected),
            "rejected": rejected[:10],
        })

        for item in rejected[:10]:
            log_person_candidate_event(
                "person_candidate_rejected",
                item,
                img.shape,
                img_path,
                item.get("reason"),
            )

        queue_rejected_review(
            img_path,
            rejected,
            "yolo_rejected",
        )

    return hits


def detect_people(img_path: Path):
    img = cv2.imread(str(img_path))
    if img is None:
        return []

    h, w = img.shape[:2]

    if has_vertical_smear_artifact(img):
        log({"event": "frame_artifact_blocked", "reason": "vertical_smear_artifact", "image": str(img_path)})
        return []

    yolo_hits = detect_people_yolo(img_path, img)
    if yolo_hits is not None:
        return yolo_hits

    blob = cv2.dnn.blobFromImage(
        cv2.resize(img, (300, 300)),
        0.007843,
        (300, 300),
        127.5,
    )

    net.setInput(blob)
    detections = net.forward()

    hits = []
    rejected = []

    for i in range(detections.shape[2]):
        conf = float(detections[0, 0, i, 2])
        class_id = int(detections[0, 0, i, 1])
        label = DNN_CLASSES[class_id] if 0 <= class_id < len(DNN_CLASSES) else str(class_id)

        if label != "person":
            continue

        box = detections[0, 0, i, 3:7] * [w, h, w, h]
        x1, y1, x2, y2 = [int(v) for v in box]
        rw, rh = x2 - x1, y2 - y1

        item = {
            "box": [x1, y1, rw, rh],
            "confidence": round(conf, 3),
        }

        threshold = current_confidence_threshold()
        item["threshold"] = threshold
        item["night_mode"] = is_night_now()
        item.update(bbox_observability_metrics(item, img.shape))
        log_person_candidate_event("person_candidate_observed", item, img.shape, img_path)
        shadow = foliage_leaf_false_positive_shadow_gate(item, img.shape)
        if shadow.get("shadow_hit"):
            log({"event": "person_candidate_shadow_foliage", "candidate": item, "shadow": shadow, "mode": "shadow_only", "image": str(img_path)})

        center_x = x1 + rw / 2.0
        center_y = y1 + rh / 2.0
        edge_margin = w * EDGE_MARGIN_RATIO
        area_ratio = (max(rw, 0) * max(rh, 0)) / float(max(w * h, 1))
        item["area_ratio"] = round(area_ratio, 3)

        if center_y < h * MIN_PERSON_CENTER_Y_RATIO:
            item["reason"] = "top_edge_false_positive"
            rejected.append(item)
            continue

        if center_x < edge_margin or center_x > (w - edge_margin):
            item["reason"] = "side_edge_false_positive"
            rejected.append(item)
            continue

        max_ignored = max((overlap_ratio((x1, y1, rw, rh), z, w, h) for z in IGNORE_ZONES), default=0.0)
        item["ignored_zone_overlap"] = round(max_ignored, 3)
        if max_ignored >= IGNORE_OVERLAP_RATIO:
            zone_override = conf >= 0.60 and area_ratio >= 0.045 and rh >= 95 and rw >= 55
            if not zone_override:
                item["reason"] = "ignored_furniture_zone"
                rejected.append(item)
                continue
            item["zone_override"] = "possible_person_in_furniture_zone"

        if conf < threshold:
            # Night borderline person candidates have caused missed real passages.
            # Keep only plausible, non-furniture-zone boxes and let downstream validation/cooldown decide.
            night_borderline = (
                item["night_mode"]
                and conf >= float(os.environ.get("BOTTAZZI_GARDEN_NIGHT_BORDERLINE_CONFIDENCE", "0.60"))
                and area_ratio >= 0.055
                and rh >= 120
                and rw >= 55
                and max_ignored < 0.20
            )
            if night_borderline:
                item["reason"] = "night_borderline_person_candidate"
                item["requires_validation"] = True
            else:
                item["reason"] = "low_confidence_night" if item["night_mode"] else "low_confidence"
                rejected.append(item)
                continue

        # blocca box generati male dal DNN, tipici su IR/ombre/arredi
        if x1 < 0 or y1 < 0 or x2 > w or y2 > h:
            item["reason"] = "box_out_of_frame"
            rejected.append(item)
            continue

        if area_ratio > MAX_BOX_AREA_RATIO:
            item["reason"] = "box_too_large"
            rejected.append(item)
            continue

        # blocca micro-falsi positivi
        if rh < MIN_PERSON_HEIGHT or rw < MIN_PERSON_WIDTH:
            item["reason"] = "too_small"
            rejected.append(item)
            continue

        if area_ratio < 0.12 and conf < SMALL_BOX_CONFIDENCE:
            item["reason"] = "small_box_low_confidence"
            rejected.append(item)
            continue

        hits.append(item)

    if rejected:
        log({"event": "detections_rejected", "count": len(rejected), "rejected": rejected[:10]})
        for item in rejected[:10]:
            log_person_candidate_event("person_candidate_rejected", item, img.shape, img_path, item.get("reason"))
            shadow = foliage_leaf_false_positive_shadow_gate(item, img.shape)
            if shadow.get("shadow_hit"):
                log({"event": "person_candidate_shadow_foliage", "candidate": item, "shadow": shadow, "mode": "shadow_only", "image": str(img_path)})
        queue_rejected_review(img_path, rejected, "dnn_rejected")

    return hits

def detect_motion_fallback(img_path: Path):
    global last_motion_frame

    img = cv2.imread(str(img_path))
    if img is None:
        return []
    img = cv2.resize(img, (480, 270))

    h, w = img.shape[:2]
    if has_vertical_smear_artifact(img):
        log({"event": "frame_artifact_blocked", "reason": "vertical_smear_artifact", "image": str(img_path)})
        return []
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (21, 21), 0)

    if last_motion_frame is None:
        last_motion_frame = gray
        return []
    if last_motion_frame.shape != gray.shape:
        log({
            "event": "motion_baseline_reset",
            "old_shape": list(last_motion_frame.shape),
            "new_shape": list(gray.shape),
        })
        last_motion_frame = gray
        return []

    delta = cv2.absdiff(last_motion_frame, gray)
    last_motion_frame = gray
    thresh = cv2.threshold(delta, 24, 255, cv2.THRESH_BINARY)[1]
    thresh = cv2.dilate(thresh, None, iterations=2)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    hits = []
    rejected = []
    fragments = []
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        area = float(cv2.contourArea(contour))
        area_ratio = area / float(max(w * h, 1))
        center_x = x + bw / 2.0
        center_y = y + bh / 2.0
        edge_margin = w * EDGE_MARGIN_RATIO
        max_ignored = max((overlap_ratio((x, y, bw, bh), z, w, h) for z in IGNORE_ZONES), default=0.0)
        item = {
            "box": [x, y, bw, bh],
            "confidence": 0.5,
            "detector": "motion_fallback",
            "area_ratio": round(area_ratio, 3),
            "ignored_zone_overlap": round(max_ignored, 3),
        }
        if center_y >= h * 0.22 and edge_margin <= center_x <= (w - edge_margin) and max_ignored < 0.50:
            fragments.append(item)

        if center_y < h * 0.10:
            item["reason"] = "motion_top_edge"
            rejected.append(item)
            continue
        if x <= 10 and y <= 25 and bw <= 70:
            item["reason"] = "motion_left_column_edge_artifact"
            rejected.append(item)
            continue
        if center_x < edge_margin or center_x > (w - edge_margin):
            item["reason"] = "motion_side_edge"
            rejected.append(item)
            continue
        if max_ignored >= IGNORE_OVERLAP_RATIO:
            item["reason"] = "motion_ignored_zone"
            rejected.append(item)
            continue
        if (x <= 5 or y <= 5 or x + bw >= w - 5 or y + bh >= h - 5) and area_ratio >= 0.10:
            item["reason"] = "motion_large_edge_artifact"
            rejected.append(item)
            continue
        if y + bh >= h - 5 and area_ratio >= 0.04:
            if x > 5 and x + bw < w - 5 and bw >= 70 and bh >= 70 and area_ratio <= 0.14:
                item["reason"] = "lower_frame_motion_possible_passage"
                hits.append(item)
                continue
            item["reason"] = "motion_bottom_smear_artifact"
            rejected.append(item)
            continue
        if bw < 45 or bh < 70 or area_ratio < 0.025:
            item["reason"] = "motion_too_small"
            rejected.append(item)
            continue
        if area_ratio > 0.45:
            item["reason"] = "motion_too_large"
            rejected.append(item)
            continue

        hits.append(item)

    if not hits:
        useful = []
        for item in fragments:
            box = item.get("box") or [0, 0, 0, 0]
            if len(box) < 4:
                continue
            x, y, bw, bh = box
            if bw >= 8 and bh >= 8:
                useful.append(item)
        if len(useful) >= 3:
            xs = [i["box"][0] for i in useful]
            ys = [i["box"][1] for i in useful]
            x2s = [i["box"][0] + i["box"][2] for i in useful]
            y2s = [i["box"][1] + i["box"][3] for i in useful]
            ux, uy, ux2, uy2 = min(xs), min(ys), max(x2s), max(y2s)
            ubw, ubh = ux2 - ux, uy2 - uy
            union_area_ratio = (ubw * ubh) / float(max(w * h, 1))
            if 0.018 <= union_area_ratio <= 0.42 and ubw >= 35 and ubh >= 45 and uy2 < h - 3:
                union_box = (ux, uy, ubw, ubh)
                union_ignored = max((overlap_ratio(union_box, z, w, h) for z in IGNORE_ZONES), default=0.0)
                if union_ignored < 0.40:
                    hits.append({
                        "box": [ux, uy, ubw, ubh],
                        "confidence": 0.5,
                        "detector": "motion_fallback",
                        "area_ratio": round(union_area_ratio, 3),
                        "ignored_zone_overlap": round(union_ignored, 3),
                        "fragment_count": len(useful),
                        "reason": "aggregated_motion_fragments",
                    })
    if rejected:
        log({"event": "motion_rejected", "count": len(rejected), "rejected": rejected[:10]})
        queue_rejected_review(img_path, rejected, "motion_rejected")
        maybe_send_gate_small_motion_review(img_path, rejected)

    return hits

def has_vertical_smear_artifact(img) -> bool:
    if img is None:
        return True
    h, w = img.shape[:2]
    if h < 80 or w < 120:
        return True
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    lower = gray[int(h * 0.42):h, :]
    if lower.size == 0:
        return False
    col_std = lower.std(axis=0)
    row_std = lower.std(axis=1)
    low_detail_cols = float((col_std < 6.0).sum()) / float(max(w, 1))
    row_variation = float(row_std.mean())
    return low_detail_cols >= 0.55 and row_variation >= 20.0

def plausible_fast_motion_passage(hits) -> bool:
    motion_hits = [h for h in hits if h.get("detector") == "motion_fallback"]
    if not motion_hits:
        return False
    plausible = []
    for h in motion_hits:
        box = h.get("box") or [0, 0, 0, 0]
        if len(box) < 4:
            continue
        x, y, bw, bh = [float(v) for v in box[:4]]
        area_ratio = float(h.get("area_ratio") or 0)
        ignored = float(h.get("ignored_zone_overlap") or 0)
        if y < 45:
            continue
        if bw >= 55 and bh >= 75 and area_ratio >= 0.025 and ignored < 0.36:
            plausible.append(h)
    if len(plausible) >= 2:
        return True
    if plausible:
        h = plausible[0]
        box = h.get("box") or [0, 0, 0, 0]
        area_ratio = float(h.get("area_ratio") or 0)
        return area_ratio >= 0.027 and float(box[3]) >= 78
    return False


def _motion_box_iou(a, b) -> float:
    try:
        ax, ay, aw, ah = [float(v) for v in a[:4]]
        bx, by, bw, bh = [float(v) for v in b[:4]]
    except Exception:
        return 0.0
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    union = max(1.0, aw * ah + bw * bh - inter)
    return inter / union


def motion_fallback_repeated_passage(hits, now_ts=None) -> tuple[bool, dict]:
    """Allow motion-only alerts only for repeated, person-like motion outside ignored zones."""
    global recent_motion_fallback_alert_candidates
    now_ts = float(now_ts or time.time())
    recent_motion_fallback_alert_candidates = [
        item for item in recent_motion_fallback_alert_candidates
        if now_ts - float(item.get("ts", 0.0)) <= 8.0
    ]
    candidates = []
    for h in hits:
        if h.get("detector") != "motion_fallback":
            continue
        box = h.get("box") or [0, 0, 0, 0]
        if len(box) < 4:
            continue
        x, y, bw, bh = [float(v) for v in box[:4]]
        area_ratio = float(h.get("area_ratio") or 0.0)
        ignored = float(h.get("ignored_zone_overlap") or 0.0)
        reason = str(h.get("reason") or "")
        aspect_h_over_w = bh / max(bw, 1.0)
        if ignored >= 0.25:
            continue
        if x <= 5 or x + bw >= 475:
            continue
        if y < 90 or y > 230:
            continue
        if not (0.024 <= area_ratio <= 0.12):
            continue
        # Low-frame repeated motion can be a real close passage even if the visible bbox is squat.
        lower_close_passage = y >= 185 and 0.024 <= area_ratio <= 0.055 and bw >= 70 and bh >= 70
        if (bw < 55 or bh < 90) and not lower_close_passage:
            continue
        # Avoid square/flat shrub motion unless it is the low-frame close-passage exception.
        if aspect_h_over_w < 1.25 and not lower_close_passage:
            continue
        if "ignored" in reason or "side_edge" in reason or "too_small" in reason:
            continue
        candidates.append(h)
    for h in candidates:
        box = h.get("box") or [0, 0, 0, 0]
        for prev in recent_motion_fallback_alert_candidates:
            if _motion_box_iou(box, prev.get("box", [0, 0, 0, 0])) >= 0.25:
                recent_motion_fallback_alert_candidates.append({"ts": now_ts, "box": box})
                return True, {"reason": "repeated_motion_fallback_person_like", "previous": prev, "candidate": h}
        recent_motion_fallback_alert_candidates.append({"ts": now_ts, "box": box})
    if len(candidates) >= 2:
        return True, {"reason": "multi_motion_fallback_person_like", "count": len(candidates), "candidates": candidates[:3]}
    return False, {"reason": "motion_fallback_not_repeated", "candidate_count": len(candidates)}

def record_event(event):
    EVENTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(EVENTS_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")

def capture_clip(dst: Path, seconds: int = 8) -> bool:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", CAMERA_RTSP_VIDEO,
        "-t", str(seconds),
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        str(dst),
    ]
    try:
        cp = subprocess.run(cmd, timeout=seconds + 15, check=False)
        return cp.returncode == 0 and dst.exists() and dst.stat().st_size > 1000
    except Exception as e:
        log({"event": "clip_error", "error": repr(e)})
        return False

def recent_citofono_confirmation(seconds: int = 180) -> dict | None:
    now = datetime.now()
    confirmations = []

    def add_confirmation(kind: str, path: Path, dt: datetime, payload: dict | None = None):
        age = abs((now - dt).total_seconds())
        if age <= seconds:
            item = dict(payload or {})
            item.update({
                "source": kind,
                "path": str(path),
                "age_seconds": round(age, 1),
            })
            confirmations.append((age, item))

    if CITOFONO_WAKE_FILE.exists():
        try:
            until = float(CITOFONO_WAKE_FILE.read_text().strip())
            if until >= time.time():
                add_confirmation(
                    "citofono_wake_marker",
                    CITOFONO_WAKE_FILE,
                    datetime.fromtimestamp(until - seconds),
                    {"seconds_left": round(until - time.time(), 1)},
                )
        except Exception:
            pass

    if CITOFONO_DOOR_MOTION_FILE.exists():
        try:
            until = float(CITOFONO_DOOR_MOTION_FILE.read_text().strip())
            if until >= time.time():
                add_confirmation(
                    "citofono_door_motion_marker",
                    CITOFONO_DOOR_MOTION_FILE,
                    datetime.fromtimestamp(until - seconds),
                    {"seconds_left": round(until - time.time(), 1), "door_motion": True},
                )
        except Exception:
            pass

    if CITOFONO_EVENTS_JSONL.exists():
        try:
            lines = CITOFONO_EVENTS_JSONL.read_text(encoding="utf-8", errors="replace").splitlines()[-80:]
        except Exception:
            lines = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("ts") or row.get("timestamp")
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").split("+")[0])
            except Exception:
                continue
            add_confirmation("citofono_events_jsonl", CITOFONO_EVENTS_JSONL, dt, row)

    if CITOFONO_WAKE_EVENTS_JSONL.exists():
        try:
            lines = CITOFONO_WAKE_EVENTS_JSONL.read_text(encoding="utf-8", errors="replace").splitlines()[-160:]
        except Exception:
            lines = []
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except Exception:
                continue
            ts = row.get("ts") or row.get("timestamp")
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00").split("+")[0])
            except Exception:
                continue
            add_confirmation("citofono_wake_events_jsonl", CITOFONO_WAKE_EVENTS_JSONL, dt, row)

    if CITOFONO_EVENTS_DIR.exists():
        cutoff = time.time() - seconds
        try:
            event_dirs = sorted(
                (p for p in CITOFONO_EVENTS_DIR.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:40]
        except Exception:
            event_dirs = []
        for event_dir in event_dirs:
            try:
                dir_mtime = event_dir.stat().st_mtime
            except Exception:
                continue
            if dir_mtime >= cutoff:
                add_confirmation(
                    "citofono_event_dir",
                    event_dir,
                    datetime.fromtimestamp(dir_mtime),
                    {"event_dir": event_dir.name},
                )
            try:
                children = list(event_dir.iterdir())
            except Exception:
                children = []
            for child in children:
                if not child.is_file():
                    continue
                if child.suffix.lower() not in CITOFONO_MEDIA_SUFFIXES and child.name != "event.json":
                    continue
                try:
                    mtime = child.stat().st_mtime
                except Exception:
                    continue
                if mtime >= cutoff:
                    add_confirmation(
                        "citofono_media",
                        child,
                        datetime.fromtimestamp(mtime),
                        {"event_dir": event_dir.name},
                    )

    if confirmations:
        return sorted(confirmations, key=lambda item: item[0])[0][1]
    return None


# FORCE_GEMMA_SUPERVISOR_TOP_EDGE_PATCH_20260613
# Anche se il supervisor approva, forza Gemma sui candidati sospetti al bordo alto/parziali.

# GARDEN_FP_FN_GEMMA_FILTER_PATCH_20260614
def _garden_fp_fn_hour():
    try:
        return datetime.now().hour
    except Exception:
        return 0


def _garden_fp_fn_hit_float(hit, key, default=0.0):
    try:
        return float(hit.get(key) or default)
    except Exception:
        return default


def garden_fp_fn_requires_vision(hits):
    hour = _garden_fp_fn_hour()
    night_start = int(os.environ.get("BOTTAZZI_GARDEN_FORCE_GEMMA_NIGHT_START", "23"))
    night_end = int(os.environ.get("BOTTAZZI_GARDEN_FORCE_GEMMA_NIGHT_END", "8"))
    night_window = hour >= night_start or hour <= night_end
    morning_suspicious = hour >= 8 and hour <= 10
    for h in hits or []:
        detector = str(h.get("detector") or "")
        box = h.get("box") or h.get("bbox") or [0, 0, 0, 0]
        y = float(box[1] if len(box) > 1 else 0)
        area = _garden_fp_fn_hit_float(h, "area_ratio")
        conf = _garden_fp_fn_hit_float(h, "confidence")
        overlap = _garden_fp_fn_hit_float(h, "ignored_zone_overlap")
        reason = str(h.get("reason") or h.get("supervisor_reason") or "")
        top_edge = y <= 20
        ambiguous_box = area < 0.12 or conf < 0.90 or overlap >= 0.20
        motion = detector == "motion_fallback"
        supervisor_fast = h.get("supervisor_approved") and h.get("supervisor_reason") == "fast_person_detected"
        artifact_like = any(x in reason for x in ["top_edge", "ignored_zone", "side_edge", "large_edge_artifact", "low_motion"])
        if motion or top_edge or artifact_like or ((night_window or morning_suspicious) and (ambiguous_box or supervisor_fast)):
            return True
    return False

def force_vision_for_supervisor_candidate(hits):
    enabled = os.environ.get("BOTTAZZI_GARDEN_FORCE_GEMMA_SUPERVISOR_TOP_EDGE", "1").strip().lower() not in ("0", "false", "no", "off")
    if not enabled:
        return False

    top_y = float(os.environ.get("BOTTAZZI_GARDEN_FORCE_GEMMA_TOP_Y", "12"))
    conf_below = float(os.environ.get("BOTTAZZI_GARDEN_FORCE_GEMMA_CONF_BELOW", "0.92"))
    area_below = float(os.environ.get("BOTTAZZI_GARDEN_FORCE_GEMMA_AREA_BELOW", "0.12"))

    for h in hits or []:
        if h.get("detector") == "motion_fallback":
            continue

        box = h.get("box") or h.get("bbox") or [999, 999, 0, 0]

        try:
            y = float(box[1])
        except Exception:
            y = 999.0

        try:
            conf = float(h.get("confidence") or 0.0)
        except Exception:
            conf = 0.0

        try:
            area = float(h.get("area_ratio") or 0.0)
        except Exception:
            area = 0.0

        if y <= top_y and (conf < conf_below or area <= area_below):
            return True

    return False




# GARDEN_PERSISTENT_LOWER_MOTION_OVERRIDE_V24_20260616
# Conservative override for repeated lower-frame motion passages that Gemma can miss as glare/smear.
def persistent_lower_frame_motion_override(hits, vision=None):
    disabled = {"0", "false", "no", "off"}
    if str(os.environ.get("BOTTAZZI_GARDEN_ENABLE_LOWER_MOTION_OVERRIDE", "0")).lower() in disabled:
        return False
    if isinstance(vision, dict):
        reason = str(vision.get("reason") or "").lower()
        try:
            vconf = float(vision.get("confidence") or 0.0)
        except Exception:
            vconf = 0.0
        hard_negative_terms = (
            "empty garden",
            "no human",
            "no humans",
            "plants",
            "furniture",
            "artifact",
            "smear",
            "shadow",
            "reflection",
        )
        if vconf >= 0.90 and any(term in reason for term in hard_negative_terms):
            log({
                "event": "motion_fallback_override_suppressed",
                "source": "garden_detector",
                "reason": "strong_negative_vision",
                "vision_reason": vision.get("reason"),
                "vision_confidence": vconf,
                "hits": hits,
            })
            return False
    plausible = []
    for h in hits or []:
        if h.get("detector") != "motion_fallback":
            continue
        box = h.get("box") or []
        if len(box) != 4:
            continue
        try:
            x, y, bw, bh = [float(v) for v in box]
            area = float(h.get("area_ratio") or 0.0)
            ignored = float(h.get("ignored_zone_overlap") or 0.0)
            fragments = int(h.get("fragment_count") or 0)
        except Exception:
            continue
        reason = str(h.get("reason") or "")
        lowerish = y >= 45 or (y + bh) >= 160 or reason == "lower_frame_motion_possible_passage"
        not_edge_artifact = x > 18 and (x + bw) < 462 and ignored < 0.36
        normal_passage_box = bw >= 70 and bh >= 60 and 0.028 <= area <= 0.18
        aggregated_passage = fragments >= 4 and bw >= 95 and bh >= 50 and 0.065 <= area <= 0.22
        if lowerish and not_edge_artifact and (normal_passage_box or aggregated_passage):
            plausible.append(h)
    if not plausible:
        return False
    repeated_gate = any(str(h.get("motion_fallback_gate") or "") == "multi_motion_fallback_person_like" for h in plausible)
    supervisor_fast = any(bool(h.get("supervisor_approved")) and str(h.get("supervisor_reason") or "") == "fast_person_detected" for h in plausible)
    multi_hit = len(plausible) >= 2
    strong_aggregated = any(int(h.get("fragment_count") or 0) >= 4 and float(h.get("area_ratio") or 0.0) >= 0.075 for h in plausible)
    return repeated_gate or supervisor_fast or multi_hit or strong_aggregated

def build_vision_evidence_image(img_path: Path, hits):
    try:
        img = cv2.imread(str(img_path))
        if img is None or not hits:
            return img_path
        h, w = img.shape[:2]
        boxes = []
        for hit in hits:
            box = hit.get("box") or hit.get("bbox") or []
            if len(box) < 4:
                continue
            x, y, bw, bh = [int(float(v)) for v in box[:4]]
            x1=max(0,x); y1=max(0,y); x2=min(w,x+max(1,bw)); y2=min(h,y+max(1,bh))
            if x2>x1 and y2>y1:
                boxes.append((x1,y1,x2,y2))
        if not boxes:
            return img_path
        x1=min(b[0] for b in boxes); y1=min(b[1] for b in boxes); x2=max(b[2] for b in boxes); y2=max(b[3] for b in boxes)
        padx=max(20,int((x2-x1)*0.35)); pady=max(20,int((y2-y1)*0.25))
        x1=max(0,x1-padx); y1=max(0,y1-pady); x2=min(w,x2+padx); y2=min(h,y2+pady)
        crop=img[y1:y2,x1:x2]
        if crop.size == 0:
            return img_path
        context=img.copy()
        for bx1,by1,bx2,by2 in boxes:
            cv2.rectangle(context,(bx1,by1),(bx2,by2),(255,255,255),2)
        target_h=max(180,min(480,h))
        context_w=max(1,int(w*target_h/h))
        crop_w=max(1,int(crop.shape[1]*target_h/crop.shape[0]))
        context=cv2.resize(context,(context_w,target_h))
        crop=cv2.resize(crop,(crop_w,target_h))
        evidence=cv2.hconcat([context,crop])
        out=Path(os.environ.get('BOTTAZZI_GARDEN_VISION_EVIDENCE_PATH', '/tmp/bottazzi-garden-vision-evidence.jpg'))
        cv2.imwrite(str(out),evidence,[int(cv2.IMWRITE_JPEG_QUALITY),92])
        return out if out.exists() and out.stat().st_size > 1000 else img_path
    except Exception as exc:
        log({"event":"vision_evidence_build_failed","error":repr(exc),"image":str(img_path)})
        return img_path


def validate_with_vision(img_path: Path, hits):
    supervisor_approved = any(h.get("supervisor_approved") for h in hits)
    has_yolo_detection = any(str(h.get("detector", "")).startswith("yolo") for h in hits)
    needs_motion_vision = any(
        h.get("detector") == "motion_fallback" and h.get("supervisor_reason") == "fast_person_detected"
        for h in hits
    )
    force_supervisor_vision = force_vision_for_supervisor_candidate(hits)

    force_fp_fn_vision = garden_fp_fn_requires_vision(hits)

    if supervisor_approved and not needs_motion_vision and not force_supervisor_vision and not force_fp_fn_vision and not (VISION_VALIDATE_YOLO and has_yolo_detection):
        return True, {"skipped": True, "reason": "supervisor_approved", "decision": hits[0].get("supervisor_reason")}

    if force_fp_fn_vision:
        log({"event": "vision_forced_fp_fn_gemma_filter", "reason": "night_morning_motion_or_ambiguous_candidate", "hits": hits})

    if supervisor_approved and not needs_motion_vision and force_supervisor_vision:
        log({
            "event": "vision_forced_supervisor_candidate",
            "reason": "top_edge_or_partial_supervisor_candidate",
            "hits": hits,
        })
    if supervisor_approved and needs_motion_vision:
        strong_fast_motion = any(
            h.get("detector") == "motion_fallback"
            and h.get("supervisor_reason") == "fast_person_detected"
            and float(h.get("supervisor_motion_score") or 0) >= 0.08
            and 0.045 <= float(h.get("area_ratio") or 0) <= 0.18
            and float(h.get("ignored_zone_overlap") or 0) < 0.20
            for h in hits
        )
        if strong_fast_motion and os.environ.get("BOTTAZZI_GARDEN_ALLOW_STRONG_FAST_MOTION_BYPASS", "0") == "1":
            return True, {"skipped": True, "reason": "strong_fast_motion_supervisor", "decision": "fast_person_detected"}
    citofono = recent_citofono_confirmation()
    if citofono and any(h.get("detector") == "motion_fallback" for h in hits):
        log({"event": "citofono_confirmed_motion_requires_vision", "citofono": citofono, "hits": hits})
    for h in hits:
        if h.get("detector") != "motion_fallback":
            continue
        box = h.get("box") or [0, 0, 0, 0]
        area_ratio = float(h.get("area_ratio") or 0)
        # GEMMA4_EXISTING_VISION_VALIDATOR_PATCH_20260613
        # Vecchio bypass troppo largo: passava anche arredo/statici grandi borderline.
        # Ora salta Gemma solo per detection molto sicure e non enormi.
        conf = float(h.get("confidence") or 0.0)
        strong_conf = float(os.environ.get("BOTTAZZI_GARDEN_VISION_STRONG_BYPASS_CONFIDENCE", "0.85"))
        strong_max_area = float(os.environ.get("BOTTAZZI_GARDEN_VISION_STRONG_BYPASS_MAX_AREA", "0.35"))
        if (
            len(box) >= 4
            and conf >= strong_conf
            and area_ratio >= 0.045
            and area_ratio <= strong_max_area
            and box[2] >= 80
            and box[3] >= 140
            and box[1] > 5
        ):
            return True, {"skipped": True, "reason": "strong_confident_person_like", "confidence": conf, "area_ratio": area_ratio}

    if any(h.get("detector") == "motion_fallback" for h in hits):
        if not VISION_VALIDATE_MOTION:
            return False, {"skipped": True, "reason": "motion_requires_supervisor_or_citofono"}
        needs_vision = True
    else:
        needs_vision = False
    if not needs_vision and VISION_VALIDATE_DNN_NIGHT:
        needs_vision = any(h.get("night_mode") for h in hits)
    if not needs_vision and VISION_VALIDATE_DNN_BORDERLINE:
        needs_vision = any(float(h.get("confidence", 0)) < VISION_BORDERLINE_CONFIDENCE for h in hits)
    if not needs_vision and VISION_VALIDATE_YOLO:
        needs_vision = any(str(h.get("detector", "")).startswith("yolo") for h in hits)
    if not needs_vision and force_supervisor_vision:
        needs_vision = True
    if not needs_vision and garden_fp_fn_requires_vision(hits):
        needs_vision = True
    if not needs_vision and any(str(h.get("supervisor_reason") or "") in {"single_frame_requires_vision", "no_motion_baseline_requires_vision"} for h in hits):
        needs_vision = True

    if not needs_vision:
        return True, {"skipped": True, "reason": "dnn_detection"}

    try:
        evidence_path = build_vision_evidence_image(img_path, hits)
        b64 = base64.b64encode(evidence_path.read_bytes()).decode("ascii")
        prompt = (
            "You are validating a security camera alert. The image may contain the full scene on the left and a zoomed candidate crop on the right. "
            "Answer only compact JSON: {\\\"human\\\":true|false,\\\"reason\\\":\\\"...\\\"}. "
            "Return human=false for corrupted frames, vertical smear, plants, shadows, furniture, empty garden. "
            "Return human=false for IR glare, insect close to lens, rain, reflections, tree branches, non-human partial objects, debris, glare, or artifacts. "
            "Return human=true if any real human body part is visible enough to identify a person, including legs, feet, torso, head, arm, or a partially occluded person at the edge of the frame. "
            "Do not reject a real person just because only legs, feet, or lower torso are visible."
        )
        # GARDEN_GEMMA_SCHEMA_STRICT_PATCH_20260617
        vision_json_schema = {
            "type": "object",
            "properties": {
                "human": {"type": "boolean"},
                "confidence": {"type": "number"},
                "reason": {"type": "string"},
            },
            "required": ["human", "confidence", "reason"],
            "additionalProperties": False,
        }

        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64}},
        ]
        payload = {
            "model": VISION_MODEL,
            "messages": [{"role": "user", "content": content}],
            "stream": False,
            "temperature": 0,
            "max_tokens": int(os.environ.get("BOTTAZZI_GARDEN_VISION_MAX_TOKENS", "64")),
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if os.environ.get("BOTTAZZI_GARDEN_VISION_JSON_SCHEMA", "1") != "0":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "garden_verdict", "strict": True, "schema": vision_json_schema},
            }
        else:
            payload["response_format"] = {"type": "json_object"}
        r = requests.post(
            f"{VISION_BASE_URL}/v1/chat/completions",
            json=payload,
            timeout=float(os.environ.get("BOTTAZZI_GARDEN_VISION_TIMEOUT_SECONDS", "90")),
        )
        r.raise_for_status()
        body = r.json()
        text = (((body.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        if not text:
            text = (((body.get("choices") or [{}])[0].get("message") or {}).get("reasoning_content") or "").strip()
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start:end + 1]) if start >= 0 and end >= start else {"raw": text}
        human_value = data.get("human")
        if human_value is None:
            human_value = data.get("person")
        human_value = bool(human_value)

        try:
            vision_confidence = float(data.get("confidence") or 0.0)
        except Exception:
            vision_confidence = 0.0

        has_motion_fallback = any((h.get("detector") == "motion_fallback") for h in (hits or []))

        if not human_value and str(data.get("reason", "")).lower().strip() == "ir glare":
            if plausible_fast_motion_passage(hits):
                return True, {"skipped": True, "reason": "plausible_fast_motion_over_ir_glare", "vision": data}

        if has_motion_fallback:
            min_motion_conf = float(os.environ.get("BOTTAZZI_GARDEN_VISION_MOTION_MIN_CONFIDENCE", "0.85"))
            if (not human_value) or vision_confidence < min_motion_conf:
                if persistent_lower_frame_motion_override(hits, data):
                    dd = dict(data)
                    dd["human"] = True
                    dd["reason"] = "persistent_lower_frame_motion_override"
                    dd["vision_original_reason"] = data.get("reason")
                    dd["vision_confidence"] = vision_confidence
                    dd["min_motion_confidence"] = min_motion_conf
                    log({
                        "event": "motion_fallback_gemma_override",
                        "source": "garden_detector",
                        "reason": "persistent_lower_frame_motion_override",
                        "vision_original_reason": data.get("reason"),
                        "vision_confidence": vision_confidence,
                        "hits": hits,
                    })
                    return True, dd
                dd = dict(data)
                dd["reason"] = "motion_fallback_not_confirmed_by_vision"
                dd["vision_confidence"] = vision_confidence
                dd["min_motion_confidence"] = min_motion_conf
                return False, dd

        return human_value, data
    except Exception as e:
        # VISION_FAIL_OPEN_DNN_HITS_PATCH_20260613
        # Se la vision remota fallisce ma c'è una hit DNN/persona, non perdere il passaggio reale.
        # Il fail-closed resta per motion_fallback puro.
        has_yolo_hit = any(str(h.get("detector", "")).startswith("yolo") for h in (hits or []))
        if has_yolo_hit and VISION_VALIDATE_YOLO:
            return False, {"error": repr(e), "reason": "vision_validation_failed_yolo_fail_closed"}
        has_dnn_hit = any(((h.get("detector") or "dnn") != "motion_fallback") for h in (hits or []))
        fail_open_dnn = os.environ.get("BOTTAZZI_GARDEN_VISION_FAIL_OPEN_DNN", "1").strip().lower() not in ("0", "false", "no", "off")
        if has_dnn_hit and fail_open_dnn and not garden_fp_fn_requires_vision(hits):
            return True, {"skipped": True, "reason": "vision_validation_failed_fail_open_dnn", "error": repr(e)}
        return False, {"error": repr(e), "reason": "vision_validation_failed"}

# PARTIAL_HUMAN_MOTION_GEMMA_PATCH_20260613
# Motion fallback viene validato da Gemma4; parti umane parziali contano come persona.


# GARDEN_CITOFONO_GARDEN_REVIEW_V23_20260615
def plausible_missed_motion_passage(hits):
    for h in hits or []:
        if h.get("detector") != "motion_fallback":
            continue
        area = float(h.get("area_ratio") or 0.0)
        fragments = int(h.get("fragment_count") or 0)
        box = h.get("box") or [0, 0, 0, 0]
        try:
            _x, y, w, height = [float(v) for v in box[:4]]
        except Exception:
            continue
        if area >= 0.035 and fragments >= 4 and w >= 60 and height >= 60 and y >= 40:
            return True
    return False


# GARDEN_GLITCH_FAILSOFT_RETRY_FIX_20260620
# Broader than the normal review gate: after a frame_grab_failed, a single
# lower-frame motion blob can be the only trace of a fast human passage.
def plausible_glitch_motion_passage(hits):
    for h in hits or []:
        if h.get("detector") != "motion_fallback":
            continue
        box = h.get("box") or [0, 0, 0, 0]
        try:
            x, y, w, height = [float(v) for v in box[:4]]
            area = float(h.get("area_ratio") or 0.0)
            ignored = float(h.get("ignored_zone_overlap") or 0.0)
        except Exception:
            continue
        reason = str(h.get("reason") or "")
        lowerish = y >= 40 or (y + height) >= 160 or reason == "lower_frame_motion_possible_passage"
        person_sized = w >= 60 and height >= 55 and 0.025 <= area <= 0.20
        not_zone_artifact = ignored < 0.40 and x > 12 and (x + w) < 468
        artifact_reason = any(
            token in reason
            for token in (
                "motion_large_edge_artifact",
                "motion_bottom_smear_artifact",
                "motion_side_edge",
                "motion_ignored_zone",
            )
        )
        if lowerish and person_sized and not_zone_artifact and not artifact_reason:
            return True
    return False


def strong_glitch_motion_passage(hits):
    strong = []
    for h in hits or []:
        if h.get("detector") != "motion_fallback":
            continue
        box = h.get("box") or [0, 0, 0, 0]
        try:
            x, y, w, height = [float(v) for v in box[:4]]
            area = float(h.get("area_ratio") or 0.0)
            ignored = float(h.get("ignored_zone_overlap") or 0.0)
        except Exception:
            continue
        reason = str(h.get("reason") or "")
        if ignored >= 0.35:
            continue
        if x <= 12 or x + w >= 468:
            continue
        if "artifact" in reason or "ignored_zone" in reason or "side_edge" in reason:
            continue
        if y <= 155 and height >= 105 and w >= 60 and 0.045 <= area <= 0.16:
            strong.append(h)
    if len(strong) >= 2:
        return True
    if len(strong) == 1:
        h = strong[0]
        box = h.get("box") or [0, 0, 0, 0]
        area = float(h.get("area_ratio") or 0.0)
        return float(box[3]) >= 125 and area >= 0.075
    return False


def send_review_motion_candidate(img_path: Path, hits, vision=None, reason="motion_fallback_rejected_but_plausible"):
    event_id = f"garden_review_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{int(time.time())}"
    payload = {
        "event_id": event_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "event": "possible_missed_passage_review",
        "reason": reason,
        "hits": hits,
        "vision": vision,
        "image": str(img_path),
        "citofono": recent_citofono_confirmation(seconds=int(os.environ.get("BOTTAZZI_GARDEN_REVIEW_CITOFONO_SECONDS", "420"))),
    }
    try:
        with REVIEW_QUEUE_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        log({"event": "review_queue_write_failed", "error": repr(exc), "payload": payload})
    caption = (
        "⚠️ Bot-tazzi giardino - possibile passaggio NON confermato\n"
        "Il detector/Gemma non lo ha confermato, ma il motion fallback è plausibile.\n"
        f"reason: {reason}\n"
        f"event_id: {event_id}\n"
        f"Ora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    try:
        with open(img_path, "rb") as fh:
            r = requests.post(
                MEOWGRAM_URL,
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"photo": fh},
                timeout=30,
            )
        log({"event": "telegram_sent", "kind": "review_motion_candidate", "status": r.status_code, "text": r.text[:500], "hits": hits, "vision": vision, "image": str(img_path), "source": "garden_detector"})
        r.raise_for_status()
        return True
    except Exception as exc:
        log({"event": "review_motion_candidate_send_failed", "error": repr(exc), "payload": payload})
        return False


# VIDEO_ASYNC_AFTER_ALERT_PATCH_20260608
def send_clip_async(event_id, clip_path):
    def _worker():
        try:
            t0 = time.time()
            clip_ok = capture_clip(clip_path, seconds=8)
            log({
                "event": "clip_capture_async_done",
                "event_id": event_id,
                "clip_ok": bool(clip_ok),
                "elapsed_ms": round((time.time() - t0) * 1000, 1),
                "clip": str(clip_path),
            })
            if not clip_ok:
                log({"event": "clip_not_sent", "event_id": event_id, "reason": "capture_failed"})
                return
            with open(clip_path, "rb") as fh:
                r = requests.post(
                    MEOWGRAM_FILE_URL,
                    data={"chat_id": CHAT_ID, "caption": f"🎥 Bot-tazzi video evento {event_id}"},
                    files={"file": fh},
                    timeout=60,
                )
            log({"event": "telegram_sent", "kind": "clip", "status": r.status_code, "text": r.text[:500]})
            r.raise_for_status()
        except Exception as e:
            log({"event": "clip_async_error", "event_id": event_id, "error": repr(e)})

    threading.Thread(target=_worker, daemon=True).start()
    log({"event": "clip_async_started", "event_id": event_id, "clip": str(clip_path)})


# TELEGRAM_SNAPSHOT_SANITIZE_BOTTOM_SMEAR_20260609
# Non cambia la detection: serve solo a non mandare su Telegram immagini con
# la parte bassa impastata/vertical smear. Se il frame è corrotto, maschera
# la fascia bassa e conserva la persona rilevata nella parte utile.
def telegram_sanitize_snapshot_if_needed(img_path):
    try:
        from pathlib import Path as _Path
        from PIL import Image, ImageStat, ImageDraw
        import statistics as _statistics

        src = _Path(img_path)
        if not src.exists():
            return img_path

        im = Image.open(src).convert("RGB")
        gray = im.convert("L")
        w, h = im.size

        if w < 100 or h < 100:
            return img_path

        cut = int(h * 0.55)
        top = gray.crop((0, 0, w, cut))
        bottom = gray.crop((0, cut, w, h))

        bsmall = bottom.resize((80, 40))
        pix = list(bsmall.getdata())

        cols = []
        for x in range(80):
            col = [pix[y * 80 + x] for y in range(40)]
            cols.append(_statistics.pstdev(col))

        col_med = _statistics.median(cols)
        top_std = ImageStat.Stat(top).stddev[0]
        bot_std = ImageStat.Stat(bottom).stddev[0]

        # Criterio largo: la parte sotto ha colonne verticali/smear o varianza
        # molto anomala rispetto alla parte alta.
        corrupted = (
            (col_med < 24 and bot_std > 22)
            or (bot_std > top_std * 1.55)
        )

        # Se esiste già un detector artefatti nel file, usalo come conferma.
        for fn_name in ("is_frame_bottom_smear_corrupted", "is_frame_artifact_corrupted", "is_frame_artifact"):
            fn = globals().get(fn_name)
            if callable(fn):
                try:
                    r = fn(src)
                    if isinstance(r, tuple):
                        corrupted = corrupted or bool(r[0])
                    elif isinstance(r, bool):
                        corrupted = corrupted or r
                except Exception:
                    pass

        if not corrupted:
            return img_path

        out_dir = src.parent / "telegram_sanitized"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / src.name

        clean = im.copy()
        d = ImageDraw.Draw(clean)

        # Maschera solo la parte bassa corrotta.
        d.rectangle([0, cut, w, h], fill=(0, 0, 0))
        d.text(
            (8, cut + 8),
            "fascia bassa mascherata: artefatto stream",
            fill=(255, 255, 255),
        )

        clean.save(out, quality=92)

        log({
            "event": "telegram_snapshot_sanitized",
            "reason": "bottom_vertical_smear",
            "source": str(src),
            "output": str(out),
            "metrics": {
                "col_med": round(float(col_med), 2),
                "top_std": round(float(top_std), 2),
                "bot_std": round(float(bot_std), 2),
            },
        })

        return out

    except Exception as e:
        try:
            log({"event": "telegram_snapshot_sanitize_error", "error": repr(e), "image": str(img_path)})
        except Exception:
            pass
        return img_path




# DAY_LARGE_LOW_MOTION_FALSE_POSITIVE_GATE_20260609
# Filtro solo giorno per falsi YOLO enormi/statici su arredo/luci:
# esempio 2026-06-09 13:41:59:
# box [199,61,280,208], confidence 0.565, area_ratio 0.449, motion_score 0.0353.
def local_box_motion_score(prev_frame, frame, box, pad_ratio=0.18):
    if prev_frame is None or frame is None or prev_frame.shape != frame.shape:
        return None
    try:
        x, y, bw, bh = [int(float(v)) for v in box[:4]]
        h, w = frame.shape[:2]
        px=max(8,int(max(1,bw)*pad_ratio)); py=max(8,int(max(1,bh)*pad_ratio))
        x1=max(0,x-px); y1=max(0,y-py); x2=min(w,x+bw+px); y2=min(h,y+bh+py)
        if x2 <= x1 or y2 <= y1:
            return None
        a=cv2.resize(prev_frame[y1:y2,x1:x2],(96,96))
        b=cv2.resize(frame[y1:y2,x1:x2],(96,96))
        diff=cv2.absdiff(a,b)
        return float(diff.mean()) / 255.0
    except Exception:
        return None


def attach_local_motion_scores(hits, prev_frame, frame):
    for hit in hits or []:
        score=local_box_motion_score(prev_frame, frame, hit.get("box") or [0,0,0,0])
        hit["local_motion_score"] = None if score is None else round(score, 4)
    return hits


def filter_day_large_low_motion_false_positive_hits(alert_hits, health=None, img_path=None):
    if not alert_hits:
        return alert_hits

    try:
        motion_score = float(getattr(health, "motion_score", 0.0) or 0.0)
    except Exception:
        motion_score = 0.0

    area_threshold = float(os.environ.get("BOTTAZZI_GARDEN_DAY_LARGE_AREA_RATIO", "0.35"))
    huge_area_threshold = float(os.environ.get("BOTTAZZI_GARDEN_DAY_HUGE_AREA_RATIO", "0.42"))
    low_motion_threshold = float(os.environ.get("BOTTAZZI_GARDEN_DAY_LOW_MOTION_SCORE", "0.06"))
    max_confidence = float(os.environ.get("BOTTAZZI_GARDEN_DAY_LARGE_MAX_CONFIDENCE", "0.70"))
    min_ignored_overlap = float(os.environ.get("BOTTAZZI_GARDEN_DAY_LARGE_IGNORED_OVERLAP", "0.15"))

    kept = []
    rejected = []

    for h in alert_hits:
        night = bool(h.get("night_mode"))
        conf = float(h.get("confidence") or 0.0)
        area = float(h.get("area_ratio") or 0.0)
        ignored_overlap = float(h.get("ignored_zone_overlap") or 0.0)

        if night:
            kept.append(h)
            continue

        large_static_low_conf = (
            area >= area_threshold
            and motion_score <= low_motion_threshold
            and conf <= max_confidence
            and (ignored_overlap >= min_ignored_overlap or area >= huge_area_threshold)
        )

        if large_static_low_conf:
            hh = dict(h)
            hh["reason"] = "day_large_low_motion_false_positive"
            hh["motion_score"] = round(motion_score, 4)
            rejected.append(hh)
        else:
            kept.append(h)

    if rejected:
        log({
            "event": "detections_rejected_day_large_low_motion_gate",
            "count": len(rejected),
            "rejected": rejected,
            "kept_count": len(kept),
            "image": str(img_path) if img_path is not None else None,
        })

    return kept



def send_alert(img_path: Path, hits):
    telegram_img_path = telegram_sanitize_snapshot_if_needed(img_path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    event_id = f"garden_{ts}_{int(time.time())}"
    event_dir = EVENTS_DIR / event_id
    event_dir.mkdir(parents=True, exist_ok=True)

    snapshot_path = event_dir / "snapshot.jpg"
    snapshot_path.write_bytes(Path(img_path).read_bytes())

    # MOTION_GEMMA_SANITIZED_STRICT_FP_PATCH_20260613
    # Usa per Gemma lo snapshot sanitizzato: evita che smear/artefatti in basso diventino falsi umani.
    vision_snapshot_path = telegram_sanitize_snapshot_if_needed(snapshot_path)
    vision_ok, vision = validate_with_vision(vision_snapshot_path, hits)
    if not vision_ok:
        event_data = {
            "event_id": event_id,
            "ts": datetime.now().isoformat(timespec="seconds"),
            "source": "garden",
            "event": "human_passage_rejected",
            "reason": "vision_rejected",
            "vision": vision,
            "hits": hits,
            "snapshot": str(snapshot_path),
        }
        (event_dir / "event.json").write_text(json.dumps(event_data, ensure_ascii=False, indent=2), encoding="utf-8")
        log({"event": "alert_rejected_by_vision", "event_id": event_id, "vision": vision})
        return False

    clip_path = event_dir / "clip.mp4"
    clip_ok = None  # video creato dopo l'alert, in background

    best = max(hits, key=lambda x: float(x.get("confidence", 0))) if hits else {}
    event_data = {
        "event_id": event_id,
        "ts": datetime.now().isoformat(timespec="seconds"),
        "source": "garden",
        "event": "human_passage",
        "identity": None,
        "direction_hint": "unknown",
        "best_confidence": best.get("confidence"),
        "hits": hits,
        "snapshot": str(snapshot_path),
        "clip": str(clip_path),
        "clip_ok": "pending_async",
    }

    (event_dir / "event.json").write_text(json.dumps(event_data, ensure_ascii=False, indent=2), encoding="utf-8")
    record_event(event_data)

    caption = (
        "🚨 Bot-tazzi giardino\n"
        "Passaggio umano rilevato\n"
        f"confidence: {best.get('confidence')}\n"
        f"event_id: {event_id}\n"
        f"Ora: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )

    with open(telegram_img_path, "rb") as fh:
        r = requests.post(
            MEOWGRAM_URL,
            data={"chat_id": CHAT_ID, "caption": caption},
            files={"photo": fh},
            timeout=30,
        )
    log({"event": "telegram_sent", "kind": "snapshot", "status": r.status_code, "text": r.text[:500], "best_candidate": enrich_person_candidate(best), "hits_count": len(hits), "snapshot": str(snapshot_path), "source": "garden_detector"})
    r.raise_for_status()

    send_clip_async(event_id, clip_path)

    return True

def main():
    global last_alert, last_review_alert, prev_supervisor_frame, prev_supervisor_ts, supervisor_frame_seq
    consecutive_grab_failures = 0
    last_stream_recovery_ts = 0.0
    last_frame_grab_failed_ts = 0.0
    log({
        "event": "garden_detector_started",
        "camera_stream": CAMERA_STREAM,
        "frame_cache_file": FRAME_CACHE_FILE,
        "frame_cache_max_age_seconds": FRAME_CACHE_MAX_AGE_SECONDS,
        "stream_recovery_owner": STREAM_RECOVERY_OWNER,
        "check_seconds": CHECK_SECONDS,
        "frame_timeout_seconds": FRAME_TIMEOUT_SECONDS,
        "max_grab_attempts": MAX_GRAB_ATTEMPTS,
        "grab_failure_backoff_seconds": GRAB_FAILURE_BACKOFF_SECONDS,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "confidence_day": DNN_CONFIDENCE_DAY,
        "confidence_night": DNN_CONFIDENCE_NIGHT,
        "yolo_enabled": YOLO_ENABLED,
        "yolo_model": YOLO_MODEL_PATH,
        "yolo_model_id": YOLO_MODEL_ID,
        "yolo_model_revision": YOLO_MODEL_REVISION,
        "yolo_model_sha256": YOLO_MODEL_SHA256,
        "yolo_detector": YOLO_DETECTOR_NAME,
        "yolo_confidence": YOLO_CONFIDENCE,
        "validate_yolo_with_gemma": VISION_VALIDATE_YOLO,
        "night_start_hour": NIGHT_START_HOUR,
        "night_end_hour": NIGHT_END_HOUR,
        "chat_id": CHAT_ID,
    })

    while True:
        now = time.time()
        img_path = SNAP_DIR / f"garden_{int(now)}.jpg"

        if grab_frame(img_path):
            # GARDEN_GLITCH_FAILSOFT_RETRY_PATCH_20260620
            recent_grab_glitch = (
                GLITCH_FAILSOFT_ENABLED
                and last_frame_grab_failed_ts > 0.0
                and (now - last_frame_grab_failed_ts) <= GLITCH_FAILSOFT_WINDOW_SECONDS
            )
            consecutive_grab_failures = 0
            hits = detect_people(img_path)
            if not hits:
                hits = detect_motion_fallback(img_path)
            log({"event": "frame_checked", "hits_count": len(hits), "hits": hits, "image": str(img_path)})
            # REAL_PERSON_ONLY_ALERT_GATE_20260608
            # motion_fallback serve solo come indizio/log: non deve mai generare alert da solo.
            # Alert consentito solo se c'è almeno un hit non-motion_fallback, cioè YOLO/person reale.
            real_person_hits = [
                h for h in hits
                if str(h.get("detector", "")).strip() != "motion_fallback"
            ]
            motion_repeat_ok, motion_repeat_info = motion_fallback_repeated_passage(hits, now)
            if hits and not real_person_hits and motion_repeat_ok:
                alert_hits = [h for h in hits if h.get("detector") == "motion_fallback"]
                for h in alert_hits:
                    h["motion_fallback_gate"] = motion_repeat_info.get("reason")
                log({
                    "event": "alert_allowed_repeated_motion_fallback_gate",
                    "reason": motion_repeat_info.get("reason"),
                    "hits_count": len(alert_hits),
                    "hits": alert_hits,
                    "image": str(img_path),
                })
            elif hits and not real_person_hits:
                vision_ok, vision = validate_with_vision(img_path, hits)
                # GARDEN_GLITCH_FAILSOFT_RETRY_PATCH_20260620
                # GARDEN_GLITCH_FAILSOFT_RETRY_FIX_20260620
                glitch_motion_plausible = recent_grab_glitch and plausible_glitch_motion_passage(hits)
                glitch_motion_reviewable = recent_grab_glitch and strong_glitch_motion_passage(hits)
                if (
                    GLITCH_FAILSOFT_ENABLED
                    and GLITCH_FAILSOFT_RETRY_ENABLED
                    and glitch_motion_plausible
                    and not vision_ok
                ):
                    log({
                        "event": "glitch_failsoft_vision_negative",
                        "reason": "recent_frame_grab_failed_before_negative_vision",
                        "window_seconds": GLITCH_FAILSOFT_WINDOW_SECONDS,
                        "last_frame_grab_failed_age_seconds": round(now - last_frame_grab_failed_ts, 3),
                        "vision": vision,
                        "hits_count": len(hits),
                        "hits": hits,
                        "image": str(img_path),
                    })
                    retry_path = SNAP_DIR / f"garden_glitch_retry_{int(time.time())}.jpg"
                    retry_grab_ok = grab_frame(retry_path)
                    retry_hits = []
                    retry_vision = None
                    if retry_grab_ok:
                        retry_hits = detect_people(retry_path)
                        if not retry_hits:
                            retry_hits = detect_motion_fallback(retry_path)
                        if retry_hits:
                            retry_ok, retry_vision = validate_with_vision(retry_path, retry_hits)
                            log({
                                "event": "glitch_failsoft_retry",
                                "retry_ok": retry_ok,
                                "retry_vision": retry_vision,
                                "retry_hits_count": len(retry_hits),
                                "retry_hits": retry_hits,
                                "retry_image": str(retry_path),
                            })
                            if retry_ok and not retry_vision.get("skipped"):
                                img_path = retry_path
                                hits = retry_hits
                                vision = retry_vision
                                vision_ok = True
                                for h in hits:
                                    h["glitch_failsoft_retry_confirmed"] = True
                                    h["motion_gemma_confirmed"] = True
                                    h["motion_gemma_gate"] = "glitch_failsoft_retry_human_confirmed"
                                log({
                                    "event": "glitch_failsoft_retry_human_confirmed",
                                    "vision": vision,
                                    "hits_count": len(hits),
                                    "hits": hits,
                                    "image": str(img_path),
                                })
                            elif (
                                (glitch_motion_reviewable or strong_glitch_motion_passage(retry_hits))
                                and now - last_review_alert >= GLITCH_FAILSOFT_REVIEW_COOLDOWN_SECONDS
                            ):
                                if send_review_motion_candidate(
                                    retry_path,
                                    retry_hits,
                                    vision=retry_vision,
                                    reason="glitch_failsoft_retry_not_confirmed",
                                ):
                                    last_review_alert = now
                                    log({
                                        "event": "glitch_failsoft_review_sent",
                                        "reason": "retry_not_confirmed",
                                        "image": str(retry_path),
                                        "vision": retry_vision,
                                        "hits_count": len(retry_hits),
                                    })
                        else:
                            log({
                                "event": "glitch_failsoft_retry_no_hits",
                                "image": str(retry_path),
                            })
                    else:
                        log({
                            "event": "glitch_failsoft_retry_grab_failed",
                            "image": str(retry_path),
                        })

                if vision_ok and not vision.get("skipped"):
                    alert_hits = hits
                    for h in alert_hits:
                        h["motion_gemma_confirmed"] = True
                        h["motion_gemma_gate"] = "human_confirmed_by_vision"
                    log({"event": "alert_allowed_motion_gemma_gate", "reason": "human_confirmed_by_vision", "vision": vision, "hits_count": len(alert_hits), "hits": alert_hits, "image": str(img_path)})
                else:
                    block_reason = motion_repeat_info.get("reason", "only_motion_fallback_no_real_person")
                    if glitch_motion_plausible:
                        block_reason = "recent_glitch_motion_fallback_not_confirmed_by_vision"
                    log({
                        "event": "alert_blocked_real_person_only_gate",
                        "reason": block_reason,
                        "vision": vision,
                        "hits_count": len(hits),
                        "hits": hits,
                        "image": str(img_path),
                    })
                    review_cooldown = float(os.environ.get("BOTTAZZI_GARDEN_REVIEW_COOLDOWN_SECONDS", "180"))
                    if (plausible_missed_motion_passage(hits) or glitch_motion_reviewable) and now - last_review_alert >= review_cooldown:
                        if send_review_motion_candidate(img_path, hits, vision=vision, reason=block_reason):
                            last_review_alert = now
                    alert_hits = []
            else:
                alert_hits = real_person_hits
            # Compute frame health before any motion-aware filtering. Previously the filters
            # saw locals().get("health") before it existed and therefore treated motion as zero.
            frame = cv2.imread(str(img_path))
            health = analyze_frame_health(prev_supervisor_frame, frame, now, prev_supervisor_ts, supervisor_freeze_state, supervisor_cfg)
            alert_hits = attach_local_motion_scores(alert_hits, prev_supervisor_frame, frame)
            alert_hits = filter_night_dynamic_cluster_memory_hits(alert_hits, health, img_path)
            alert_hits = filter_day_large_low_motion_false_positive_hits(alert_hits, health, img_path)
            alert_allowed = False
            if alert_hits:
                dets = []
                for h in alert_hits:
                    box = h.get("box") or [0, 0, 0, 0]
                    x, y, bw, bh = box
                    dets.append({"confidence": float(h.get("confidence") or 0), "box": [x, y, x + bw, y + bh], "detector": str(h.get("detector") or "unknown"), "local_motion_score": h.get("local_motion_score")})
                decision = person_supervisor.decide(supervisor_frame_seq, now, dets, health, frame.shape if frame is not None else None)
                supervisor_frame_seq += 1
                log({"event": "person_supervisor", "status": decision.status, "reason": decision.reason, "send_alert": decision.send_alert, "track_id": decision.track_id, "motion_score": round(decision.motion_score, 4)})
                if decision.send_alert:
                    alert_allowed = True
                    for h in alert_hits:
                        h["supervisor_approved"] = True
                        h["supervisor_reason"] = decision.reason
                        h["supervisor_motion_score"] = round(decision.motion_score, 4)
                        h["track_id"] = decision.track_id
                elif decision.reason == "single_frame_requires_vision":
                    # Preserve recall for a fast one-frame passage, but never alert directly:
                    # force the VLM verifier to arbitrate this ambiguous case.
                    alert_allowed = True
                    for h in alert_hits:
                        h["supervisor_reason"] = "single_frame_requires_vision"
                        h["supervisor_motion_score"] = round(decision.motion_score, 4)
                        h["track_id"] = decision.track_id
                        h["supervisor_confidence_median"] = round(decision.confidence_median, 4)
                        h["supervisor_confidence_max"] = round(decision.confidence_max, 4)
                    log({"event": "alert_deferred_to_vision", "reason": "single_frame_requires_vision", "track_id": decision.track_id})
                elif decision.reason == "single_frame_low_motion" and prev_supervisor_frame is None:
                    # First usable frame after restart has no motion baseline. Do not approve it here;
                    # allow the existing vision validator to decide so real fast passages are not lost.
                    alert_allowed = True
                    for h in alert_hits:
                        h["supervisor_reason"] = "no_motion_baseline_requires_vision"
                        h["track_id"] = decision.track_id
                    log({"event": "alert_deferred_to_vision", "reason": "no_motion_baseline", "track_id": decision.track_id})
                elif any(h.get("motion_gemma_confirmed") for h in alert_hits):
                    alert_allowed = True
                    for h in alert_hits:
                        h["supervisor_approved"] = True
                        h["supervisor_reason"] = "gemma_confirmed_motion_fallback"
                        h["supervisor_motion_score"] = round(decision.motion_score, 4)
                        h["track_id"] = decision.track_id
                    log({"event": "alert_allowed_after_gemma_motion_supervisor", "reason": decision.reason, "track_id": decision.track_id})
                else:
                    log({"event": "alert_blocked_by_supervisor", "status": decision.status, "reason": decision.reason, "track_id": decision.track_id})

            if alert_hits and alert_allowed:
                alert_hits = filter_furniture_zone_false_positive_hits(alert_hits, img_path)
                if not alert_hits:
                    alert_allowed = False

            if alert_hits and alert_allowed and now - last_alert >= COOLDOWN_SECONDS:
                if alert_hits and send_alert(img_path, alert_hits):
                    last_alert = now
            elif alert_hits and alert_allowed:
                log({"event": "alert_skipped_cooldown", "seconds_left": round(COOLDOWN_SECONDS - (now - last_alert), 1)})

            prev_supervisor_frame = frame
            prev_supervisor_ts = now
        else:
            consecutive_grab_failures += 1
            # GARDEN_GLITCH_FAILSOFT_RETRY_PATCH_20260620
            last_frame_grab_failed_ts = time.time()
            backoff = GRAB_FAILURE_BACKOFF_SECONDS if consecutive_grab_failures >= 2 else CHECK_SECONDS
            log({"event": "frame_grab_failed", "consecutive": consecutive_grab_failures, "backoff_seconds": backoff})
            now_mono = time.monotonic()
            if consecutive_grab_failures >= STREAM_RECOVERY_FAILURES and (now_mono - last_stream_recovery_ts) >= STREAM_RECOVERY_COOLDOWN_SECONDS:
                last_stream_recovery_ts = now_mono
                if STREAM_RECOVERY_OWNER == "watchdog":
                    log({
                        "event": "stream_recovery_delegated",
                        "consecutive": consecutive_grab_failures,
                        "owner": "watchdog",
                        "action": "none",
                    })
                else:
                    log({"event": "stream_stale_or_grab_fail_recovery", "consecutive": consecutive_grab_failures, "cooldown_seconds": STREAM_RECOVERY_COOLDOWN_SECONDS, "action": "docker_restart_go2rtc"})
                    try:
                        proc = subprocess.run(["docker", "restart", "go2rtc"], text=True, capture_output=True, timeout=30)
                        log({"event": "stream_recovery_action_result", "cmd": "docker restart go2rtc", "returncode": proc.returncode, "stdout": proc.stdout.strip(), "stderr": proc.stderr.strip()})
                        time.sleep(3.0)
                    except Exception as exc:
                        log({"event": "stream_recovery_action_error", "cmd": "docker restart go2rtc", "error": repr(exc)})
            time.sleep(backoff)
            continue

        time.sleep(CHECK_SECONDS)



# NIGHT_DYNAMIC_CLUSTER_MEMORY_GATE_20260609
# Filtro solo-notte, dinamico: non dipende dalla posizione della Vespa.
# Ogni nuovo cluster YOLO/persona notturno ha una finestra iniziale libera.
# Se continua a ripetersi nello stesso cluster oltre la finestra, viene trattato
# come oggetto statico e non genera più alert.
night_dynamic_clusters = []


def _night_dynamic_iou_xywh(a, b):
    try:
        ax, ay, aw, ah = [float(x) for x in a[:4]]
        bx, by, bw, bh = [float(x) for x in b[:4]]
    except Exception:
        return 0.0

    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh

    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)

    iw = max(0.0, ix2 - ix1)
    ih = max(0.0, iy2 - iy1)

    inter = iw * ih
    union = max(1.0, aw * ah + bw * bh - inter)
    return inter / union


def _night_dynamic_avg_box(old_box, new_box, n):
    try:
        return [
            (float(old_box[i]) * (n - 1) + float(new_box[i])) / n
            for i in range(4)
        ]
    except Exception:
        return new_box


def filter_night_dynamic_cluster_memory_hits(alert_hits, health=None, img_path=None):
    global night_dynamic_clusters

    if not alert_hits:
        return alert_hits

    import time as _time

    now_ts = _time.time()

    # Parametri prudenti. Solo notte.
    allow_seconds = float(os.environ.get("BOTTAZZI_GARDEN_NIGHT_CLUSTER_ALLOW_SECONDS", "240"))
    cluster_iou = float(os.environ.get("BOTTAZZI_GARDEN_NIGHT_CLUSTER_IOU", "0.45"))
    max_cluster_idle_seconds = float(os.environ.get("BOTTAZZI_GARDEN_NIGHT_CLUSTER_IDLE_SECONDS", "21600"))

    try:
        motion_score = float(getattr(health, "motion_score", 0.0) or 0.0)
    except Exception:
        motion_score = 0.0

    # pulizia cluster vecchi
    night_dynamic_clusters = [
        c for c in night_dynamic_clusters
        if now_ts - float(c.get("last_seen", 0.0)) <= max_cluster_idle_seconds
    ]

    kept = []
    rejected = []

    for h in alert_hits:
        night = bool(h.get("night_mode"))
        box = h.get("box") or [0, 0, 0, 0]
        conf = float(h.get("confidence") or 0.0)

        # Giorno invariato.
        if not night:
            kept.append(h)
            continue

        # Se è un passaggio molto forte e con tanto movimento globale,
        # non bloccare: possibile persona reale.
        if conf >= 0.97 and motion_score >= 0.10:
            kept.append(h)
            continue

        best = None
        best_iou = 0.0

        for c in night_dynamic_clusters:
            v = _night_dynamic_iou_xywh(box, c.get("box", [0, 0, 0, 0]))
            if v > best_iou:
                best_iou = v
                best = c

        if best is None or best_iou < cluster_iou:
            best = {
                "id": len(night_dynamic_clusters) + 1,
                "box": [float(x) for x in box[:4]],
                "first_seen": now_ts,
                "last_seen": now_ts,
                "count": 1,
                "max_conf": conf,
            }
            night_dynamic_clusters.append(best)
        else:
            best["count"] = int(best.get("count", 0)) + 1
            best["last_seen"] = now_ts
            best["max_conf"] = max(float(best.get("max_conf", 0.0)), conf)
            best["box"] = _night_dynamic_avg_box(best.get("box", box), box, int(best["count"]))

        age = now_ts - float(best.get("first_seen", now_ts))

        if age <= allow_seconds:
            hh = dict(h)
            hh["night_dynamic_cluster_id"] = best.get("id")
            hh["night_dynamic_cluster_age"] = round(age, 1)
            hh["night_dynamic_cluster_count"] = int(best.get("count", 0))
            kept.append(hh)
        else:
            hh = dict(h)
            hh["reason"] = "night_dynamic_cluster_repeat"
            hh["night_dynamic_cluster_id"] = best.get("id")
            hh["night_dynamic_cluster_age"] = round(age, 1)
            hh["night_dynamic_cluster_count"] = int(best.get("count", 0))
            hh["motion_score"] = round(motion_score, 4)
            rejected.append(hh)

    if rejected:
        log({
            "event": "detections_rejected_night_dynamic_cluster_memory",
            "count": len(rejected),
            "rejected": rejected,
            "kept_count": len(kept),
            "clusters": [
                {
                    "id": c.get("id"),
                    "count": c.get("count"),
                    "age": round(now_ts - float(c.get("first_seen", now_ts)), 1),
                    "box": [round(float(x), 1) for x in c.get("box", [0, 0, 0, 0])],
                }
                for c in night_dynamic_clusters
            ],
            "image": str(img_path) if img_path is not None else None,
        })

    return kept


if __name__ == "__main__":
    main()
