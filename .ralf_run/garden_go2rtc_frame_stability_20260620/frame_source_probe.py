#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


LAB_DIR = Path(__file__).resolve().parent
SAMPLES_DIR = LAB_DIR / "samples"
DEFAULT_FRAME_URL = "http://127.0.0.1:1984/api/frame.jpeg?src=camera_giardino_anteriore"
DEFAULT_WARMER_FILE = "/run/bottazzi-garden-warmer-frame.jpg"
DEFAULT_RTSP_URL = "rtsp://127.0.0.1:8555/camera_giardino_anteriore"


@dataclass
class Candidate:
    name: str
    kind: str
    target: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_optional_modules() -> dict[str, Any]:
    mods: dict[str, Any] = {"Image": None, "np": None}
    try:
        from PIL import Image

        mods["Image"] = Image
    except Exception as exc:
        mods["PIL_error"] = repr(exc)
    try:
        import numpy as np

        mods["np"] = np
    except Exception as exc:
        mods["numpy_error"] = repr(exc)
    return mods


def fetch_http(url: str, timeout_s: float) -> dict[str, Any]:
    started = time.perf_counter()
    status = None
    data = b""
    error = None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "bottazzi-frame-source-probe/1"})
        with urllib.request.urlopen(req, timeout=timeout_s) as res:
            status = int(getattr(res, "status", 0) or 0)
            data = res.read()
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        error = f"HTTPError: {exc}"
        try:
            data = exc.read()
        except Exception:
            data = b""
    except TimeoutError as exc:
        error = f"TimeoutError: {exc}"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "status": status,
        "elapsed_s": round(time.perf_counter() - started, 4),
        "size_bytes": len(data),
        "error": error,
        "data": data,
    }


def fetch_file(path_s: str) -> dict[str, Any]:
    started = time.perf_counter()
    path = Path(path_s)
    data = b""
    error = None
    age_s = None
    try:
        st = path.stat()
        age_s = max(0.0, time.time() - st.st_mtime)
        data = path.read_bytes()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    return {
        "status": 200 if data else None,
        "elapsed_s": round(time.perf_counter() - started, 4),
        "size_bytes": len(data),
        "error": error,
        "file_age_s": round(age_s, 3) if age_s is not None else None,
        "data": data,
    }


def fetch_ffmpeg(rtsp_url: str, timeout_s: float, out_path: Path) -> dict[str, Any]:
    started = time.perf_counter()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-frames:v",
        "1",
        "-q:v",
        "3",
        "-y",
        str(out_path),
    ]
    error = None
    try:
        cp = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout_s)
        if cp.returncode != 0:
            error = (cp.stderr or cp.stdout or f"ffmpeg_returncode:{cp.returncode}").strip()[:500]
    except subprocess.TimeoutExpired:
        error = "TimeoutExpired"
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    data = b""
    if out_path.exists():
        data = out_path.read_bytes()
    return {
        "status": 200 if data and not error else None,
        "elapsed_s": round(time.perf_counter() - started, 4),
        "size_bytes": len(data),
        "error": error,
        "data": data,
    }


def jpeg_metrics(data: bytes, mods: dict[str, Any]) -> dict[str, Any]:
    Image = mods.get("Image")
    np = mods.get("np")
    if not data:
        return {"jpeg_valid": False, "width": None, "height": None, "smear_score": None, "possible_smear": True, "note": "empty"}
    if Image is None:
        return {"jpeg_valid": False, "width": None, "height": None, "smear_score": None, "possible_smear": None, "note": "PIL_unavailable"}
    try:
        import io

        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            gray_img = img.convert("L")
            if np is None:
                return {
                    "jpeg_valid": True,
                    "width": int(width),
                    "height": int(height),
                    "smear_score": None,
                    "possible_smear": None,
                    "note": "numpy_unavailable",
                }
            arr = np.asarray(gray_img, dtype="float32")
    except Exception as exc:
        return {
            "jpeg_valid": False,
            "width": None,
            "height": None,
            "smear_score": None,
            "possible_smear": True,
            "note": f"invalid_jpeg:{type(exc).__name__}",
        }

    if arr.ndim != 2 or min(arr.shape) < 16:
        return {"jpeg_valid": True, "width": int(width), "height": int(height), "smear_score": None, "possible_smear": True, "note": "invalid_dimensions"}

    step_y = max(1, arr.shape[0] // 360)
    step_x = max(1, arr.shape[1] // 640)
    small = arr[::step_y, ::step_x]
    hdiff = np.abs(np.diff(small, axis=1))
    vdiff = np.abs(np.diff(small, axis=0))
    h_mean = float(np.mean(hdiff)) if hdiff.size else 0.0
    v_mean = float(np.mean(vdiff)) if vdiff.size else 0.0
    h_over_v = h_mean / (v_mean + 1e-6)
    v_over_h = v_mean / (h_mean + 1e-6)
    std = float(np.std(small))
    adjacent_col_diffs = np.mean(hdiff, axis=0) if hdiff.size else np.asarray([], dtype="float32")
    repeated_cols = float(np.mean(adjacent_col_diffs < max(0.75, std * 0.012))) if adjacent_col_diffs.size else 0.0
    col_means = np.mean(small, axis=0)
    med = float(np.median(col_means))
    mad = float(np.median(np.abs(col_means - med))) + 1e-6
    col_zmax = float(np.max(np.abs(col_means - med)) / (1.4826 * mad))
    score = max(h_over_v / 4.0, v_over_h / 4.0, repeated_cols * 6.0, max(0.0, (col_zmax - 8.0) / 5.0))
    possible = bool(score >= 1.0 or repeated_cols >= 0.18 or h_over_v >= 4.0 or v_over_h >= 4.0 or col_zmax >= 12.0)
    reasons = []
    if repeated_cols >= 0.18:
        reasons.append("repeated_columns")
    if h_over_v >= 4.0:
        reasons.append("horizontal_diff_dominates")
    if v_over_h >= 4.0:
        reasons.append("vertical_diff_dominates")
    if col_zmax >= 12.0:
        reasons.append("vertical_bands")
    return {
        "jpeg_valid": True,
        "width": int(width),
        "height": int(height),
        "smear_score": round(float(score), 4),
        "possible_smear": possible,
        "h_diff_mean": round(h_mean, 4),
        "v_diff_mean": round(v_mean, 4),
        "h_over_v": round(float(h_over_v), 4),
        "v_over_h": round(float(v_over_h), 4),
        "repeated_cols_fraction": round(repeated_cols, 4),
        "column_band_zmax": round(col_zmax, 4),
        "gray_std": round(std, 4),
        "note": "+".join(reasons) if reasons else "ok",
    }


def percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = (len(ordered) - 1) * q
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return ordered[int(idx)]
    return ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo)


def summarize(rows: list[dict[str, Any]], candidate: Candidate) -> dict[str, Any]:
    mine = [r for r in rows if r["candidate"] == candidate.name]
    times = [float(r["elapsed_s"]) for r in mine if r.get("elapsed_s") is not None]
    sizes = [int(r["size_bytes"]) for r in mine if int(r.get("size_bytes") or 0) > 0]
    jpeg_valid = [r for r in mine if r.get("jpeg_valid") is True]
    smear = [r for r in mine if r.get("possible_smear") is True]
    http_ok = [r for r in mine if r.get("status") == 200 and not r.get("error")]
    timeouts = [r for r in mine if "Timeout" in str(r.get("error") or "")]
    file_ages = [float(r["file_age_s"]) for r in mine if r.get("file_age_s") is not None]
    return {
        "name": candidate.name,
        "kind": candidate.kind,
        "target": candidate.target,
        "attempts": len(mine),
        "ok": len(http_ok),
        "errors": sum(1 for r in mine if r.get("error") or r.get("status") not in (200, None)),
        "timeouts": len(timeouts),
        "avg_time_s": round(statistics.mean(times), 4) if times else None,
        "p50_time_s": round(percentile(times, 0.50), 4) if times else None,
        "p95_time_s": round(percentile(times, 0.95), 4) if times else None,
        "max_time_s": round(max(times), 4) if times else None,
        "size_avg_bytes": round(statistics.mean(sizes), 1) if sizes else None,
        "valid_jpeg": len(jpeg_valid),
        "smear_glitch": len(smear),
        "smear_rate": round(len(smear) / len(mine), 4) if mine else None,
        "file_age_avg_s": round(statistics.mean(file_ages), 3) if file_ages else None,
        "file_age_p95_s": round(percentile(file_ages, 0.95), 3) if file_ages else None,
        "file_age_max_s": round(max(file_ages), 3) if file_ages else None,
    }


def write_markdown(path: Path, output_json: Path, summary: dict[str, Any]) -> None:
    lines = [
        f"# Frame Source Probe: {summary['label']}",
        "",
        f"- generated_at: `{summary['generated_at']}`",
        f"- attempts_per_candidate: `{summary['attempts_per_candidate']}`",
        f"- timeout_seconds: `{summary['timeout_seconds']}`",
        f"- interval_seconds: `{summary['interval_seconds']}`",
        f"- json: `{output_json.name}`",
        "",
        "| source | kind | attempts | ok | errors | timeouts | avg_s | p50_s | p95_s | max_s | valid_jpeg | smear/glitch | smear_rate | file_age_p95_s |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in summary["sources"]:
        def fmt(v: Any) -> str:
            if v is None:
                return "n/a"
            return str(v)

        lines.append(
            "| {name} | {kind} | {attempts} | {ok} | {errors} | {timeouts} | {avg} | {p50} | {p95} | {maxv} | {valid} | {smear} | {rate} | {age} |".format(
                name=item["name"],
                kind=item["kind"],
                attempts=item["attempts"],
                ok=item["ok"],
                errors=item["errors"],
                timeouts=item["timeouts"],
                avg=fmt(item["avg_time_s"]),
                p50=fmt(item["p50_time_s"]),
                p95=fmt(item["p95_time_s"]),
                maxv=fmt(item["max_time_s"]),
                valid=item["valid_jpeg"],
                smear=item["smear_glitch"],
                rate=fmt(item["smear_rate"]),
                age=fmt(item["file_age_p95_s"]),
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def parse_candidate(raw: str) -> Candidate:
    name, rest = raw.split("=", 1)
    kind, target = rest.split(":", 1)
    if kind not in {"http", "file", "ffmpeg_rtsp"}:
        raise argparse.ArgumentTypeError(f"unsupported candidate kind: {kind}")
    return Candidate(name=name.strip(), kind=kind, target=target.strip())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Bottazzi garden frame sources.")
    parser.add_argument("--label", default="before", choices=["before", "after", "custom"])
    parser.add_argument("--attempts", type=int, default=80)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--interval", type=float, default=0.2)
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--candidate", action="append", type=parse_candidate)
    parser.add_argument("--include-ffmpeg-rtsp", action="store_true")
    return parser.parse_args()


def default_candidates(include_ffmpeg: bool) -> list[Candidate]:
    candidates = [
        Candidate("go2rtc_frame", "http", DEFAULT_FRAME_URL),
    ]
    if Path(DEFAULT_WARMER_FILE).exists():
        candidates.append(Candidate("warmer_cache_file", "file", DEFAULT_WARMER_FILE))
    if include_ffmpeg and shutil.which("ffmpeg"):
        candidates.append(Candidate("go2rtc_rtsp_ffmpeg_one_shot", "ffmpeg_rtsp", DEFAULT_RTSP_URL))
    return candidates


def main() -> int:
    args = parse_args()
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    candidates = args.candidate or default_candidates(args.include_ffmpeg_rtsp)
    mods = load_optional_modules()
    json_path = LAB_DIR / f"{args.label}_probe.json"
    md_path = LAB_DIR / f"{args.label}_probe.md"
    jsonl_path = LAB_DIR / "results.jsonl"
    rows: list[dict[str, Any]] = []
    sample_counts = {c.name: 0 for c in candidates}

    with jsonl_path.open("a", encoding="utf-8") as out:
        for attempt in range(1, int(args.attempts) + 1):
            for candidate in candidates:
                sample_path = SAMPLES_DIR / candidate.name / f"{args.label}_{attempt:04d}.jpg"
                data = b""
                if candidate.kind == "http":
                    result = fetch_http(candidate.target, args.timeout)
                    data = result.pop("data")
                elif candidate.kind == "file":
                    result = fetch_file(candidate.target)
                    data = result.pop("data")
                else:
                    result = fetch_ffmpeg(candidate.target, args.timeout, sample_path)
                    data = result.pop("data")
                metrics = jpeg_metrics(data, mods)
                saved_sample = None
                if data and sample_counts[candidate.name] < args.sample_limit:
                    sample_path.parent.mkdir(parents=True, exist_ok=True)
                    sample_path.write_bytes(data)
                    sample_counts[candidate.name] += 1
                    saved_sample = str(sample_path)
                row = {
                    "label": args.label,
                    "ts": utc_now(),
                    "attempt": attempt,
                    "candidate": candidate.name,
                    "kind": candidate.kind,
                    "target": candidate.target,
                    "sample_path": saved_sample,
                    **result,
                    **metrics,
                }
                rows.append(row)
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
            if attempt == 1 or attempt % 10 == 0:
                print(json.dumps({"event": "progress", "label": args.label, "attempt": attempt}, ensure_ascii=False), flush=True)
            if args.interval > 0 and attempt < int(args.attempts):
                time.sleep(args.interval)

    summary = {
        "label": args.label,
        "generated_at": utc_now(),
        "attempts_per_candidate": int(args.attempts),
        "timeout_seconds": float(args.timeout),
        "interval_seconds": float(args.interval),
        "optional_import_errors": {k: v for k, v in mods.items() if k.endswith("_error")},
        "sources": [summarize(rows, c) for c in candidates],
    }
    json_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_markdown(md_path, json_path, summary)
    print(json.dumps({"event": "done", "label": args.label, "json": str(json_path), "md": str(md_path)}, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
