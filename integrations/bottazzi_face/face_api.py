from pathlib import Path
import os
import pickle
import tempfile
import subprocess

import cv2
import numpy as np
import face_recognition
from fastapi import FastAPI, UploadFile, File, Header, HTTPException
from pydantic import BaseModel

try:
    import insight_engine
except Exception:
    insight_engine = None

TOKEN = os.environ.get("BOTTAZZI_TOKEN", "")
BASE = Path.home() / "bottazzi-face"
DB_PATH = Path(os.environ.get("BOTTAZZI_FACE_DLIB_DB", str(BASE / "encodings.pkl"))).expanduser()
CASCADE_PATH = "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml"
PROFILE_CASCADE_PATH = "/usr/share/opencv4/haarcascades/haarcascade_profileface.xml"
FACE_MAX_Y_RATIO = float(os.environ.get("BOTTAZZI_FACE_MAX_Y_RATIO", "0.78"))

app = FastAPI(title="Bot-tazzi Face API")


class VideoPathReq(BaseModel):
    path: str


class ImagePathReq(BaseModel):
    path: str


def check_auth(x_bottazzi_token: str | None):
    if TOKEN and x_bottazzi_token != TOKEN:
        raise HTTPException(status_code=401, detail="bad token")


def load_db():
    with open(DB_PATH, "rb") as f:
        known = pickle.load(f)
    return [x["name"] for x in known], [x["encoding"] for x in known]


def recognize_image_path(img_path: Path, min_size=120, threshold=0.50):
    known_names, known_encs = load_db()
    cascade = cv2.CascadeClassifier(CASCADE_PATH)
    profile_cascade = cv2.CascadeClassifier(PROFILE_CASCADE_PATH)

    bgr = cv2.imread(str(img_path))
    if bgr is None:
        raise ValueError(f"immagine non leggibile: {img_path}")

    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    ih, iw = rgb.shape[:2]

    faces_front = cascade.detectMultiScale(
        gray,
        scaleFactor=1.03,
        minNeighbors=3,
        minSize=(25, 25),
    )

    faces_profile = profile_cascade.detectMultiScale(
        gray,
        scaleFactor=1.08,
        minNeighbors=3,
        minSize=(25, 25),
    )

    gray_flip = cv2.flip(gray, 1)
    faces_profile_flip = profile_cascade.detectMultiScale(
        gray_flip,
        scaleFactor=1.08,
        minNeighbors=3,
        minSize=(25, 25),
    )

    fw = gray.shape[1]
    flipped_fixed = []
    for (x, y, w, h) in faces_profile_flip:
        flipped_fixed.append((fw - x - w, y, w, h))

    faces = list(faces_front) + list(faces_profile) + flipped_fixed

    results = []
    for (x, y, w, h) in faces:
        if w < 40 or h < 40:
            continue
        # Tuya can corrupt the lower strip of the frame. Never treat that strip as a face.
        if (float(y) + float(h) * 0.5) / max(float(ih), 1.0) > FACE_MAX_Y_RATIO:
            continue

        pad = int(max(w, h) * 0.35)
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(iw, x + w + pad)
        y2 = min(ih, y + h + pad)

        crop = rgb[y1:y2, x1:x2]
        crop = np.ascontiguousarray(crop)

        loc = [(0, crop.shape[1], crop.shape[0], 0)]
        encs = face_recognition.face_encodings(crop, known_face_locations=loc, num_jitters=1)
        if not encs:
            continue

        distances = face_recognition.face_distance(known_encs, encs[0])
        best_i = int(np.argmin(distances))
        best_name = known_names[best_i]
        best_distance = float(distances[best_i])

        if best_distance <= threshold:
            match = best_name
        elif best_distance <= 0.60:
            match = f"forse_{best_name}"
        else:
            match = "sconosciuto"

        results.append({
            "match": match,
            "best_name": best_name,
            "distance": round(best_distance, 4),
            "box": [int(x), int(y), int(w), int(h)],
            "crop_box": [int(x1), int(y1), int(x2), int(y2)],
        })

    insight = {"ok": False, "reason": "disabled", "faces": []}
    if insight_engine is not None:
        try:
            insight = insight_engine.recognize(img_path)
            for face in insight.get("faces", []):
                if face.get("match") and face["match"] != "sconosciuto":
                    results.append(face)
        except Exception as exc:
            insight = {"ok": False, "reason": str(exc), "faces": []}

    return {
        "image": str(img_path),
        "opencv_faces": int(len(faces)),
        "insightface": insight,
        "results": results,
    }


def summarize_video(video_path: Path):
    hits = []
    checked = 0

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        pattern = td / "frame_%03d.jpg"

        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video_path),
            "-vf", "fps=0.5",
            str(pattern),
        ], check=True)

        for img_path in sorted(td.glob("frame_*.jpg")):
            checked += 1
            if insight_engine is not None:
                insight = insight_engine.recognize(img_path)
                res = {"results": insight.get("faces", [])}
            else:
                res = recognize_image_path(img_path)
            for r in res["results"]:
                if r["match"] and not r["match"].startswith("forse_") and r["match"] != "sconosciuto":
                    hits.append({
                        "frame": img_path.name,
                        **r,
                    })

    by_name = {}
    for h in hits:
        by_name.setdefault(h["match"], []).append(h["distance"])

    summary = []
    verdict = "nessun_volto_riconosciuto"

    for name, ds in sorted(by_name.items()):
        row = {
            "name": name,
            "count": len(ds),
            "avg_distance": round(sum(ds) / len(ds), 4),
            "best_distance": round(min(ds), 4),
        }
        summary.append(row)

    if summary:
        best = max(summary, key=lambda x: x["count"])
        if best["count"] >= 3 and best["avg_distance"] <= 0.48:
            verdict = best["name"]
        elif best["count"] >= 1 and best["best_distance"] <= 0.30:
            verdict = best["name"]
        else:
            verdict = "incerto"

    return {
        "video": str(video_path),
        "frames_checked": checked,
        "hits": hits,
        "summary": summary,
        "verdict": verdict,
    }


@app.get("/health")
def health():
    return {"ok": True, "db_exists": DB_PATH.exists()}


@app.get("/known_people")
def known_people(x_bottazzi_token: str | None = Header(default=None)):
    check_auth(x_bottazzi_token)
    names, _ = load_db()
    return {"ok": True, "people": sorted(set(names)), "encodings": len(names)}


@app.post("/recognize_image")
async def recognize_image(file: UploadFile = File(...), x_bottazzi_token: str | None = Header(default=None)):
    check_auth(x_bottazzi_token)
    suffix = Path(file.filename or "image.jpg").suffix or ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        return {"ok": True, **recognize_image_path(tmp_path)}
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/recognize_image_path")
def recognize_image_path_api(req: ImagePathReq, x_bottazzi_token: str | None = Header(default=None)):
    check_auth(x_bottazzi_token)
    p = Path(req.path).expanduser().resolve()
    allowed = [Path("/opt/bottazzi-citofono").resolve(), Path("/opt/bottazzi-garden").resolve()]
    if not any(p.is_relative_to(root) for root in allowed):
        raise HTTPException(status_code=403, detail="path non consentito")
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"file non trovato: {p}")
    return {"ok": True, **recognize_image_path(p)}


@app.post("/recognize_video_path")
def recognize_video_path(req: VideoPathReq, x_bottazzi_token: str | None = Header(default=None)):
    check_auth(x_bottazzi_token)
    p = Path(req.path).expanduser()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"file non trovato: {p}")
    return {"ok": True, **summarize_video(p)}
