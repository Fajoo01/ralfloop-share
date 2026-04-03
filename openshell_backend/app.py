from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

BASE_DIR = Path("/home/sibilla-cumana/ralfloop_agent_scaffold/.openshell_backend")
BASE_DIR.mkdir(parents=True, exist_ok=True)

AUDIT_LOG = BASE_DIR / "audit.jsonl"

DESTRUCTIVE_PATTERNS = [
    "rm -rf",
    "mkfs",
    "shutdown",
    "reboot",
    "dd ",
]

app = FastAPI(title="Ralfloop OpenShell Backend")


def audit(event: str, **fields) -> None:
    row = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def sandbox_root(sid: str) -> Path:
    return BASE_DIR / sid / "workspace"


def resolve_in_sandbox(root: Path, rel_path: str) -> Path:
    rel = rel_path.lstrip("/")
    full = (root / rel).resolve()
    if root.resolve() not in full.parents and full != root.resolve():
        raise HTTPException(status_code=403, detail="path_not_allowed")
    return full


def check_command_allowed(command: str) -> tuple[bool, str]:
    lowered = command.lower()
    for pat in DESTRUCTIVE_PATTERNS:
        if pat in lowered:
            return False, f"destructive_command:{pat.strip()}"
    return True, "allowed"


class SandboxCreateResponse(BaseModel):
    id: str
    root: str
    status: str


class WriteRequest(BaseModel):
    path: str
    content: str


class ExecRequest(BaseModel):
    command: str
    timeout_sec: int = 20


@app.post("/sandboxes", response_model=SandboxCreateResponse)
def create_sandbox():
    sid = str(uuid.uuid4())
    root = sandbox_root(sid)
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    audit("sandbox_created", sandbox_id=sid, root=str(root))
    return SandboxCreateResponse(id=sid, root=str(root), status="ready")


@app.delete("/sandboxes/{sid}")
def destroy_sandbox(sid: str):
    target = BASE_DIR / sid
    if target.exists():
        shutil.rmtree(target)
    audit("sandbox_destroyed", sandbox_id=sid)
    return {"ok": True}


@app.get("/sandboxes/{sid}/list")
def list_dir(sid: str, path: str = Query(".")):
    root = sandbox_root(sid)
    if not root.exists():
        raise HTTPException(status_code=404, detail="sandbox_not_found")

    full = resolve_in_sandbox(root, path)

    if not full.exists():
        raise HTTPException(status_code=404, detail="path_not_found")
    if not full.is_dir():
        raise HTTPException(status_code=400, detail="not_a_directory")

    entries = [f"{'d' if p.is_dir() else 'f'} {p.name}" for p in sorted(full.iterdir())]
    audit("list_dir", sandbox_id=sid, path=path, entries_count=len(entries))
    return {"ok": True, "path": path, "entries": entries}


@app.get("/sandboxes/{sid}/read")
def read_file(sid: str, path: str = Query(...)):
    root = sandbox_root(sid)
    if not root.exists():
        raise HTTPException(status_code=404, detail="sandbox_not_found")

    full = resolve_in_sandbox(root, path)

    if not full.exists():
        raise HTTPException(status_code=404, detail="path_not_found")
    if not full.is_file():
        raise HTTPException(status_code=400, detail="not_a_file")

    content = full.read_text(encoding="utf-8")
    audit("read_file", sandbox_id=sid, path=path, size=len(content))
    return {"ok": True, "path": path, "content": content}


@app.post("/sandboxes/{sid}/write")
def write_file(sid: str, payload: WriteRequest):
    root = sandbox_root(sid)
    if not root.exists():
        raise HTTPException(status_code=404, detail="sandbox_not_found")

    full = resolve_in_sandbox(root, payload.path)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(payload.content, encoding="utf-8")
    audit("write_file", sandbox_id=sid, path=payload.path, size=len(payload.content))
    return {"ok": True, "path": payload.path, "written": True}


@app.post("/sandboxes/{sid}/exec")
def exec_in_sandbox(sid: str, payload: ExecRequest):
    root = sandbox_root(sid)
    if not root.exists():
        raise HTTPException(status_code=404, detail="sandbox_not_found")

    allowed, reason = check_command_allowed(payload.command)
    if not allowed:
        audit("exec_denied", sandbox_id=sid, command=payload.command, reason=reason)
        raise HTTPException(status_code=403, detail=reason)

    try:
        proc = subprocess.run(
            ["/bin/bash", "-lc", payload.command],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=payload.timeout_sec,
        )
        audit(
            "exec",
            sandbox_id=sid,
            command=payload.command,
            exit_code=proc.returncode,
            stdout_len=len(proc.stdout or ""),
            stderr_len=len(proc.stderr or ""),
        )
        return {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    except subprocess.TimeoutExpired as e:
        audit("exec_timeout", sandbox_id=sid, command=payload.command, timeout_sec=payload.timeout_sec)
        return {
            "ok": False,
            "exit_code": 124,
            "stdout": e.stdout or "",
            "stderr": e.stderr or "command timed out",
        }


@app.get("/audit")
def get_audit(limit: int = Query(100, ge=1, le=1000)):
    if not AUDIT_LOG.exists():
        return {"ok": True, "events": []}

    lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    rows = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            rows.append({"event": "invalid_json_line", "raw": line})
    return {"ok": True, "events": rows}


@app.get("/sandboxes")
def list_sandboxes():
    rows = []
    for d in sorted(BASE_DIR.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith("."):
            continue
        root = d / "workspace"
        if not root.exists():
            continue
        rows.append(
            {
                "id": d.name,
                "root": str(root),
                "status": "ready",
            }
        )
    return {"ok": True, "sandboxes": rows}


@app.get("/sandboxes/{sid}")
def inspect_sandbox(sid: str):
    root = sandbox_root(sid)
    if not root.exists():
        raise HTTPException(status_code=404, detail="sandbox_not_found")

    out_dir = root / "out"
    tmp_dir = root / "tmp"

    file_count = 0
    dir_count = 0
    total_bytes = 0

    for p in root.rglob("*"):
        if p.is_dir():
            dir_count += 1
        elif p.is_file():
            file_count += 1
            try:
                total_bytes += p.stat().st_size
            except Exception:
                pass

    payload = {
        "ok": True,
        "sandbox": {
            "id": sid,
            "root": str(root),
            "status": "ready",
            "exists": True,
            "out_exists": out_dir.exists(),
            "tmp_exists": tmp_dir.exists(),
            "file_count": file_count,
            "dir_count": dir_count,
            "total_bytes": total_bytes,
        },
    }
    audit("inspect_sandbox", sandbox_id=sid, file_count=file_count, dir_count=dir_count, total_bytes=total_bytes)
    return payload

import re
import requests
from bs4 import BeautifulSoup


class ProbeStreamRequest(BaseModel):
    url: str
    referer: str | None = None
    user_agent: str | None = None


def _video_headers(referer: str | None = None, user_agent: str | None = None) -> dict[str, str]:
    headers = {
        "User-Agent": user_agent or "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36"
    }
    if referer:
        headers["Referer"] = referer
    return headers


def _guess_stream_type(url: str) -> str:
    lower = url.lower()
    if ".m3u8" in lower:
        return "hls"
    if ".mpd" in lower:
        return "dash"
    if lower.endswith(".mp4"):
        return "mp4"
    return "unknown"


def _extract_candidate_urls(text: str) -> list[str]:
    patterns = [
        r'https?://[^"\'>\s]+\.m3u8[^"\'>\s]*',
        r'https?://[^"\'>\s]+\.mpd[^"\'>\s]*',
        r'https?://[^"\'>\s]+\.mp4[^"\'>\s]*',
        r'//[^"\'>\s]+\.m3u8[^"\'>\s]*',
        r'//[^"\'>\s]+\.mpd[^"\'>\s]*',
        r'//[^"\'>\s]+\.mp4[^"\'>\s]*',
        r'/[^"\'>\s]+\.m3u8[^"\'>\s]*',
        r'/[^"\'>\s]+\.mpd[^"\'>\s]*',
        r'/[^"\'>\s]+\.mp4[^"\'>\s]*',
    ]
    found = []
    for pat in patterns:
        found.extend(re.findall(pat, text, flags=re.IGNORECASE))
    dedup = []
    seen = set()
    for u in found:
        if u not in seen:
            seen.add(u)
            dedup.append(u)
    return dedup


def _parse_hls_manifest(text: str) -> dict:
    variants = []
    audios = []
    subtitles = []
    current_inf = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-STREAM-INF:"):
            current_inf = line
            continue
        if current_inf and not line.startswith("#"):
            variants.append({"info": current_inf, "uri": line})
            current_inf = None
            continue
        if line.startswith("#EXT-X-MEDIA:"):
            if "TYPE=AUDIO" in line:
                audios.append(line)
            if "TYPE=SUBTITLES" in line:
                subtitles.append(line)

    drm_suspected = any(
        tag in text
        for tag in [
            "#EXT-X-KEY",
            "com.widevine",
            "com.microsoft.playready",
            "skd://",
            "urn:uuid",
        ]
    )

    return {
        "variant_count": len(variants),
        "audio_count": len(audios),
        "subtitle_count": len(subtitles),
        "variants": variants[:20],
        "audios": audios[:20],
        "subtitles": subtitles[:20],
        "drm_suspected": drm_suspected,
    }


def _parse_dash_manifest(text: str) -> dict:
    drm_suspected = any(
        tag.lower() in text.lower()
        for tag in [
            "contentprotection",
            "widevine",
            "playready",
            "clearkey",
            "cenc:pssh",
        ]
    )
    adaptation_sets = len(re.findall(r"<AdaptationSet\b", text, flags=re.IGNORECASE))
    representations = len(re.findall(r"<Representation\b", text, flags=re.IGNORECASE))
    return {
        "adaptation_set_count": adaptation_sets,
        "representation_count": representations,
        "drm_suspected": drm_suspected,
    }


@app.post("/sandboxes/{sid}/probe_stream")
def probe_stream(sid: str, payload: ProbeStreamRequest):
    root = sandbox_root(sid)
    if not root.exists():
        raise HTTPException(status_code=404, detail="sandbox_not_found")

    headers = _video_headers(payload.referer, payload.user_agent)
    url = payload.url
    kind = _guess_stream_type(url)

    result = {
        "ok": True,
        "input_url": url,
        "final_url": url,
        "kind": kind,
        "headers_used": headers,
        "reachable": False,
        "http_status": None,
        "drm_suspected": False,
        "manifest": {},
        "page_candidates": [],
        "notes": [],
    }

    try:
        r = requests.get(url, headers=headers, timeout=20)
        result["http_status"] = r.status_code
        result["final_url"] = str(r.url)
        result["reachable"] = 200 <= r.status_code < 300
    except Exception as e:
        audit("probe_stream_error", sandbox_id=sid, url=url, error=str(e))
        raise HTTPException(status_code=400, detail=f"fetch_failed:{e}")

    content_type = r.headers.get("Content-Type", "")
    body = r.text

    if kind == "hls" or ".m3u8" in result["final_url"].lower() or "mpegurl" in content_type.lower():
        parsed = _parse_hls_manifest(body)
        result["kind"] = "hls"
        result["manifest"] = parsed
        result["drm_suspected"] = parsed["drm_suspected"]
        result["notes"].append("manifest_hls_parsed")

    elif kind == "dash" or ".mpd" in result["final_url"].lower() or "dash+xml" in content_type.lower():
        parsed = _parse_dash_manifest(body)
        result["kind"] = "dash"
        result["manifest"] = parsed
        result["drm_suspected"] = parsed["drm_suspected"]
        result["notes"].append("manifest_dash_parsed")

    elif kind == "mp4" or "video/mp4" in content_type.lower():
        result["kind"] = "mp4"
        result["notes"].append("direct_mp4_detected")

    else:
        soup = BeautifulSoup(body, "lxml")
        html_text = str(soup)
        candidates = _extract_candidate_urls(html_text)
        normalized = []
        from urllib.parse import urljoin
        base = str(r.url)
        for c in candidates:
            normalized.append(urljoin(base, c))
        dedup = []
        seen = set()
        for c in normalized:
            if c not in seen:
                seen.add(c)
                dedup.append(c)
        result["page_candidates"] = dedup[:30]
        result["notes"].append("page_scanned_for_video_candidates")

        lower = body.lower()
        if any(x in lower for x in ["widevine", "playready", "contentprotection", "fairplay"]):
            result["drm_suspected"] = True
            result["notes"].append("drm_markers_found_in_page")

    out_dir = root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "probe_stream.json"
    out_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    audit(
        "probe_stream",
        sandbox_id=sid,
        url=url,
        kind=result["kind"],
        reachable=result["reachable"],
        drm_suspected=result["drm_suspected"],
        candidates=len(result["page_candidates"]),
    )

    return {
        **result,
        "artifact": "out/probe_stream.json",
    }
