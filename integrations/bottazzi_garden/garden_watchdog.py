#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.request import urlopen

import cv2
import numpy as np

FRAME_URL = os.getenv("BOTTAZZI_GARDEN_FRAME_URL", "http://127.0.0.1:1984/api/frame.jpeg?src=camera_giardino_anteriore")
CACHE_PATH = Path(os.getenv("BOTTAZZI_GARDEN_FRAME_CACHE_FILE", "/run/bottazzi-garden-warmer-frame.jpg"))
STATE_PATH = Path(os.getenv("BOTTAZZI_GARDEN_WATCHDOG_STATE", "/opt/bottazzi-garden/garden_watchdog_state.json"))
LOCK_PATH = Path(os.getenv("BOTTAZZI_GARDEN_WATCHDOG_LOCK", "/run/bottazzi-garden-watchdog.lock"))
CACHE_MAX_AGE = float(os.getenv("BOTTAZZI_GARDEN_FRAME_CACHE_MAX_AGE_SECONDS", "15"))
RESTART_SCORE = int(os.getenv("BOTTAZZI_GARDEN_WATCHDOG_RESTART_SCORE", "4"))
RESTART_COOLDOWN = float(os.getenv("BOTTAZZI_GARDEN_WATCHDOG_RESTART_COOLDOWN_SECONDS", "600"))


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


def inspect_jpeg(data: bytes) -> dict:
    result = {"ok": False, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()[:16] if data else ""}
    if len(data) < 1000:
        result["error"] = "empty_frame"
        return result
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        result["error"] = "decoded_empty_frame"
        return result
    result["shape"] = list(img.shape)
    if has_vertical_smear_artifact(img):
        result["error"] = "vertical_smear_artifact"
        return result
    result["ok"] = True
    return result


def probe_direct(timeout: float = 7.0) -> dict:
    try:
        with urlopen(FRAME_URL, timeout=timeout) as response:
            if response.status != 200:
                return {"ok": False, "error": f"http_{response.status}"}
            return inspect_jpeg(response.read(2_000_000))
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def probe_cache() -> dict:
    try:
        st = CACHE_PATH.stat()
        age = time.time() - st.st_mtime
        if age > CACHE_MAX_AGE:
            return {"ok": False, "error": "cache_stale", "age_seconds": round(age, 2), "bytes": st.st_size}
        result = inspect_jpeg(CACHE_PATH.read_bytes())
        result["age_seconds"] = round(age, 2)
        return result
    except FileNotFoundError:
        return {"ok": False, "error": "cache_missing"}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


def load_state() -> dict:
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def run_restart() -> dict:
    cp = subprocess.run(["docker", "restart", "go2rtc"], text=True, capture_output=True, timeout=40)
    return {"returncode": cp.returncode, "stdout": cp.stdout[-200:].strip(), "stderr": cp.stderr[-200:].strip()}


def evaluate(previous: dict, direct: dict, cache: dict, now: float) -> dict:
    healthy_count = int(bool(direct.get("ok"))) + int(bool(cache.get("ok")))
    previous_score = int(previous.get("degraded_score") or 0)
    if healthy_count == 2:
        score = max(0, previous_score - 1)
    elif healthy_count == 1:
        score = min(RESTART_SCORE + 2, previous_score + 1)
    else:
        score = min(RESTART_SCORE + 2, previous_score + 2)
    last_restart = float(previous.get("last_restart_epoch") or 0.0)
    restart_due = score >= RESTART_SCORE and now - last_restart >= RESTART_COOLDOWN
    return {"degraded_score": score, "restart_due": restart_due, "healthy_count": healthy_count}


def main() -> int:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "skipped", "reason": "watchdog_already_running"}))
            return 0

        previous = load_state()
        now = time.time()
        direct = probe_direct()
        cache = probe_cache()
        decision = evaluate(previous, direct, cache, now)
        state = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "direct": direct,
            "cache": cache,
            **decision,
            "last_restart_epoch": float(previous.get("last_restart_epoch") or 0.0),
            "action": "none",
        }
        if decision["restart_due"]:
            state["action"] = "restart_go2rtc"
            state["restart"] = run_restart()
            state["last_restart_epoch"] = now
            if state["restart"]["returncode"] == 0:
                time.sleep(5.0)
                state["post_restart_direct"] = probe_direct()
                state["post_restart_cache"] = probe_cache()
                state["degraded_score"] = 0 if state["post_restart_direct"].get("ok") else 1
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
        print(json.dumps(state, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
