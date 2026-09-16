from __future__ import annotations

import json
import os
from pathlib import Path

import cv2
import numpy as np
from insightface.app import FaceAnalysis

BASE = Path.home() / "bottazzi-face"
DB = Path(os.environ.get("BOTTAZZI_FACE_INSIGHT_DB", str(BASE / "faces_insight.json"))).expanduser()
FACE_MAX_Y_RATIO = float(os.environ.get("BOTTAZZI_FACE_MAX_Y_RATIO", "0.78"))
_app = None
_db_mtime = None
_known = []


def _cosine(a, b):
    return float(np.dot(a, b) / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-9))


def _load_app():
    global _app
    if _app is None:
        _app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
        _app.prepare(ctx_id=-1, det_size=(640, 640))
    return _app


def _enhance_variants(img):
    variants = [("original", img)]

    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    lab2 = cv2.merge((clahe.apply(l), a, b))
    variants.append(("clahe", cv2.cvtColor(lab2, cv2.COLOR_LAB2BGR)))

    for gamma, name in ((0.65, "gamma_dark"), (1.35, "gamma_light")):
        table = np.array([((i / 255.0) ** gamma) * 255 for i in range(256)]).astype("uint8")
        variants.append((name, cv2.LUT(img, table)))

    blur = cv2.GaussianBlur(img, (0, 0), 1.0)
    variants.append(("sharpen", cv2.addWeighted(img, 1.6, blur, -0.6, 0)))
    return variants


def _crop_face(img, bbox, pad_ratio=0.45):
    h, w = img.shape[:2]
    x1, y1, x2, y2 = [float(v) for v in bbox]
    bw, bh = x2 - x1, y2 - y1
    pad = max(bw, bh) * pad_ratio
    x1 = max(0, int(x1 - pad))
    y1 = max(0, int(y1 - pad))
    x2 = min(w, int(x2 + pad))
    y2 = min(h, int(y2 + pad))
    if x2 <= x1 or y2 <= y1:
        return None, None
    return img[y1:y2, x1:x2], [x1, y1, x2, y2]


def _load_known():
    global _db_mtime, _known
    if not DB.exists():
        return []
    mtime = DB.stat().st_mtime
    if _db_mtime == mtime:
        return _known
    data = json.loads(DB.read_text())
    known = []
    for name, rows in data.get("people", {}).items():
        for row in rows:
            known.append((name, np.array(row["embedding"], dtype="float32")))
    _known = known
    _db_mtime = mtime
    return _known


def recognize(img_path: Path):
    known = _load_known()
    if not known:
        return {"ok": False, "reason": "no_insight_db", "faces": []}
    img = cv2.imread(str(img_path))
    if img is None:
        return {"ok": False, "reason": "image_not_readable", "faces": []}
    app = _load_app()
    best_by_box = {}
    save_debug = str(os.environ.get("BOTTAZZI_FACE_SAVE_DEBUG_CROPS", "0")).strip().lower() in {"1", "true", "yes", "on"}
    debug_dir = None
    if save_debug:
        configured = os.environ.get("BOTTAZZI_FACE_DEBUG_CROP_DIR")
        debug_dir = Path(configured).expanduser() if configured else img_path.parent / "face_crops"
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            debug_dir = None

    for variant_name, variant in _enhance_variants(img):
        trusted_h = max(1, min(variant.shape[0], int(round(variant.shape[0] * FACE_MAX_Y_RATIO))))
        trusted = variant[:trusted_h, :]
        for face in app.get(trusted):
            emb = face.embedding.astype("float32")
            scores = []
            for name in sorted(set(n for n, _ in known)):
                vals = [_cosine(emb, e) for n, e in known if n == name]
                scores.append({
                    "name": name,
                    "best": max(vals),
                    "avg_top3": sum(sorted(vals, reverse=True)[:3]) / min(3, len(vals)),
                })
            scores_sorted = sorted(scores, key=lambda x: x["best"], reverse=True)
            best = scores_sorted[0] if scores_sorted else None
            second = scores_sorted[1] if len(scores_sorted) > 1 else None
            if not best:
                verdict = "sconosciuto"
                best_name = None
                best_score = 0.0
            else:
                best_name = best["name"]
                best_score = float(best["best"])
                if best["best"] >= 0.42 and best["avg_top3"] >= 0.35:
                    verdict = best_name
                elif best["best"] >= 0.32:
                    verdict = f"forse_{best_name}"
                else:
                    verdict = "sconosciuto"

            bbox = [round(float(x), 1) for x in face.bbox.tolist()]
            crop_path = None
            crop_box = None
            crop, crop_box = _crop_face(img, face.bbox)
            if debug_dir is not None and crop is not None and crop.size:
                safe = f"{img_path.stem}_{variant_name}_{len(best_by_box)}.jpg"
                crop_path = str(debug_dir / safe)
                cv2.imwrite(crop_path, crop)

            row = {
                "match": verdict,
                "best_name": best_name,
                "score": round(best_score, 4),
                "distance": round(1.0 - best_score, 4),
                "second_name": second["name"] if second else None,
                "second_score": round(float(second["best"]), 4) if second else None,
                "margin": round(best_score - float(second["best"]), 4) if second else None,
                "avg_top3": round(float(best.get("avg_top3", 0.0)), 4) if best else None,
                "det_score": round(float(face.det_score), 4),
                "box": bbox,
                "crop_box": crop_box,
                "crop": crop_path,
                "engine": "insightface",
                "variant": variant_name,
            }
            key = tuple(int(round(v / 24.0)) for v in bbox)
            old = best_by_box.get(key)
            if old is None or (row["score"], row["det_score"]) > (old["score"], old["det_score"]):
                best_by_box[key] = row

    out = []
    for row in sorted(best_by_box.values(), key=lambda r: (r["score"], r["det_score"]), reverse=True):
        out.append(row)

    # Legacy fallback path kept only for clarity: variants above now cover original frame too.
    if False:
        for face in app.get(img):
            pass
    return {"ok": True, "engine": "insightface", "faces": out}
