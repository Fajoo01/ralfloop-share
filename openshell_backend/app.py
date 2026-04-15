from __future__ import annotations

import json
import shutil
import subprocess
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel
from openshell_backend.common_router import route_common, maybe_autopromote_candidate
from openshell_backend.skills.responses import build_skill_insufficient_response
from openshell_backend.contracts.coder_autofix_runner import maybe_attach_coder_text
from openshell_backend.contracts.coder_patch_apply import maybe_apply_and_validate_coder_patch, apply_coder_patch_permanently

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

BACKEND_VENV_BIN = os.path.expanduser("~/ralfloop_agent_scaffold/.venv/bin")


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
        env = os.environ.copy()
        env["PATH"] = BACKEND_VENV_BIN + os.pathsep + env.get("PATH", "")
        proc = subprocess.run(
            ["/bin/bash", "-lc", payload.command],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=payload.timeout_sec,
            env=env,
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

from typing import Optional, Dict, Any
from pydantic import BaseModel
from openshell_backend.skills.registry import try_direct_skill
from openshell_backend.skills.validators import is_skill_output_sufficient, validate_skill_output

TASK_RUN_DEFAULTS = {
    "planner_model_profile": "generalist",
    "coder_model_profile": "coder",
    "judge_model_profile": "generalist",
    "planner_model_name": "qwen2.5:7b",
    "coder_model_name": "qwen2.5:7b",
    "judge_model_name": "qwen2.5:7b",
    "planner_rag_collection": "ralfloop_planner",
    "coder_rag_collection": "ralfloop_coder",
    "judge_rag_collection": "ralfloop_judge",
    "internal_prompt_style": "standard",
}


class TaskRunRequest(BaseModel):
    user_goal: str
    mode: str = "planner_coder_judge"
    planner_model_profile: Optional[str] = None
    coder_model_profile: Optional[str] = None
    judge_model_profile: Optional[str] = None

    planner_model_name: Optional[str] = None
    coder_model_name: Optional[str] = None
    judge_model_name: Optional[str] = None

    planner_rag_collection: Optional[str] = None
    coder_rag_collection: Optional[str] = None
    judge_rag_collection: Optional[str] = None
    internal_prompt_style: Optional[str] = None

    skill_context: Optional[str] = None
    extra_context: Optional[Dict[str, Any]] = None



def _task_run_selection(req: TaskRunRequest) -> dict[str, dict[str, str]]:
    effective = {
        key: getattr(req, key) if getattr(req, key) is not None else default
        for key, default in TASK_RUN_DEFAULTS.items()
    }
    return {
        "profiles": {
            "planner": effective["planner_model_profile"],
            "coder": effective["coder_model_profile"],
            "judge": effective["judge_model_profile"],
        },
        "models": {
            "planner": effective["planner_model_name"],
            "coder": effective["coder_model_name"],
            "judge": effective["judge_model_name"],
        },
        "rag": {
            "planner": effective["planner_rag_collection"],
            "coder": effective["coder_rag_collection"],
            "judge": effective["judge_rag_collection"],
        },
        "internal_prompt_style": effective["internal_prompt_style"],
    }



def _probe_mediaset_from_source_page(channel_id: str, source_page: str) -> dict:
    import re as _re
    import requests as _requests

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Referer": "https://mediasetinfinity.mediaset.it/",
        "Origin": "https://mediasetinfinity.mediaset.it",
    }

    if not source_page:
        return {
            "ok": False,
            "stage": "probe_html_api",
            "channel_id": channel_id,
            "source_page": source_page,
            "error": "missing_source_page",
        }

    try:
        r = _requests.get(source_page, headers=headers, timeout=(3, 6))
        html = r.text or ""
    except Exception as e:
        return {
            "ok": False,
            "stage": "probe_html_api",
            "channel_id": channel_id,
            "source_page": source_page,
            "error": f"source_fetch_error:{e}",
        }

    candidates = []
    candidates.extend(_extract_mediaset_candidate_urls(html))

    html_unescaped = html.replace(r"\/", "/")
    if html_unescaped != html:
        candidates.extend(_extract_mediaset_candidate_urls(html_unescaped))

    seen = set()
    uniq = []
    for url in candidates:
        if not url:
            continue
        url = url.strip().strip('"').strip("'")
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("/"):
            continue
        if "manifest.mpd" not in url and not url.endswith(".mpd"):
            continue
        if url not in seen:
            seen.add(url)
            uniq.append(url)

    for url in uniq:
        checked = _validate_manifest_url(url, headers=headers)
        if checked.get("ok"):
            checked["stage"] = "probe_html_api"
            checked["channel_id"] = channel_id
            checked["source_page"] = source_page
            checked["candidate_count"] = len(uniq)
            return checked

    return {
        "ok": False,
        "stage": "probe_html_api",
        "channel_id": channel_id,
        "source_page": source_page,
        "error": "no_valid_manifest_found",
        "candidate_count": len(uniq),
        "candidates_sample": uniq[:5],
    }

def _extract_mediaset_candidate_urls(html: str) -> list[str]:
    import re as _re

    if not html:
        return []

    patterns = [
        r'https://[^"\'\s]+?manifest\.mpd[^"\'\s]*',
        r'https:\\/\\/[^"\'\s]+?manifest\.mpd[^"\'\s]*',
        r'https://[^"\'\s]+?\.mpd[^"\'\s]*',
        r'https:\\/\\/[^"\'\s]+?\.mpd[^"\'\s]*',
        r'//[^"\'\s]+?manifest\.mpd[^"\'\s]*',
        r'//[^"\'\s]+?\.mpd[^"\'\s]*',
    ]

    out = []
    for pat in patterns:
        out.extend(_re.findall(pat, html, flags=_re.IGNORECASE))

    cleaned = []
    seen = set()
    for url in out:
        url = url.replace("\\/", "/")
        if url.startswith("//"):
            url = "https:" + url
        if url not in seen:
            seen.add(url)
            cleaned.append(url)

    return cleaned

def _validate_manifest_url(url: str, headers: dict | None = None) -> dict:
    import requests as _requests

    headers = headers or {}

    try:
        r = _requests.get(url, headers=headers, timeout=12)
        body = r.text[:1200] if r.text else ""
        ok = (
            r.status_code == 200
            and (
                "<MPD" in body
                or "urn:mpeg:dash:schema:mpd" in body
                or "AdaptationSet" in body
            )
        )
        return {
            "ok": ok,
            "stage": "probe_manifest_validation",
            "url": url,
            "headers": headers,
            "status_code": r.status_code,
            "body_sample": body[:200],
        }
    except Exception as e:
        return {
            "ok": False,
            "stage": "probe_manifest_validation",
            "url": url,
            "headers": headers,
            "error": f"manifest_fetch_error:{e}",
        }


def _probe_mediaset_api_graph_capture(channel_id: str) -> dict:
    import json as _json
    import re as _re
    import requests as _requests
    from pathlib import Path as _Path

    if str(channel_id).lower() != "canale5":
        return {
            "ok": False,
            "stage": "probe_api_graph_capture",
            "channel_id": channel_id,
            "error": "unsupported_channel_for_capture_probe",
        }

    capture_file = _Path(f"/tmp/{channel_id.lower()}_api_graph.jsonl")
    if not capture_file.exists():
        return {
            "ok": False,
            "stage": "probe_api_graph_capture",
            "channel_id": channel_id,
            "error": "missing_capture_file",
            "capture_file": str(capture_file),
        }

    try:
        line = capture_file.read_text(encoding="utf-8").splitlines()[0]
        obj = _json.loads(line)
        url = obj.get("url") or ""
        raw_headers = obj.get("req_headers") or {}
    except Exception as e:
        return {
            "ok": False,
            "stage": "probe_api_graph_capture",
            "channel_id": channel_id,
            "error": f"capture_parse_error:{e}",
        }

    keep = [
        "Authorization",
        "x-m-device-id",
        "x-m-sid",
        "x-m-platform",
        "x-m-property",
        "x-m-user-context",
        "x-m-app-version",
        "User-Agent",
        "Referer",
        "content-type",
    ]
    headers = {k: v for k, v in raw_headers.items() if k in keep and v}

    try:
        r = _requests.get(url, headers=headers, timeout=15)
        body = r.text or ""
    except Exception as e:
        return {
            "ok": False,
            "stage": "probe_api_graph_capture",
            "channel_id": channel_id,
            "api_graph_url": url,
            "error": f"api_graph_fetch_error:{e}",
        }

    def _grab(pat: str):
        m = _re.search(pat, body)
        return m.group(1) if m else None

    return {
        "ok": r.status_code == 200 and (f'"callSign":"{channel_id}"' in body or '"callSign":"' in body),
        "stage": "probe_api_graph_capture",
        "channel_id": channel_id,
        "api_graph_url": url,
        "status_code": r.status_code,
        "headers": headers,
        "callSign": _grab(r'"callSign":"([^"]+)"'),
        "cardTitle": _grab(r'"cardTitle":"([^"]+)"'),
        "guid": _grab(r'"guid":"([^"]+)"'),
        "title": _grab(r'"title":"([^"]+)"'),
        "liveAllowed": _grab(r'"liveAllowed":(true|false)'),
        "restartAllowed": _grab(r'"restartAllowed":(true|false)'),
        "startTime": _grab(r'"startTime":"([^"]+)"'),
        "endTime": _grab(r'"endTime":"([^"]+)"'),
        "body_sample": body[:1000],
    }

def _legacy_channels_out_fallback(channel_id: str) -> dict:
    return {
        "ok": False,
        "stage": "probe_cache_fallback",
        "channel_id": channel_id,
        "fallback": "legacy_channels_out_json",
    }




def _mediaset_anonymous_login_headers(user_agent: str = "") -> dict:
    import requests as _requests

    _ua = user_agent or "Mozilla/5.0"
    _url = "https://api-ott-prod-fe.mediaset.net/PROD/play/idm/anonymous/login/v2.0"
    _headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "Referer": "https://mediasetinfinity.mediaset.it/",
        "User-Agent": _ua,
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }
    _payload = {
        "appName": "web//mediasetplay-web/1.1.0-509a584"
    }

    try:
        _r = _requests.post(_url, headers=_headers, json=_payload, timeout=20)
        _j = _r.json() if (_r.text or "").strip() else {}
    except Exception as e:
        print("[STREAM_PROBE] anonymous_login=", {"ok": False, "error": f"request_error:{e}"}, flush=True)
        return {
            "ok": False,
            "stage": "anonymous_login",
            "error": f"request_error:{e}",
        }

    _resp = (_j or {}).get("response") or {}
    _token = _resp.get("beToken")
    _sid = _resp.get("sid")

    out = {
        "ok": _r.status_code == 200 and bool(_token) and bool(_sid),
        "stage": "anonymous_login",
        "status_code": _r.status_code,
        "has_token": bool(_token),
        "has_sid": bool(_sid),
        "headers": {
            "Authorization": f"Bearer {_token}" if _token else "",
            "x-m-sid": _sid or "",
            "User-Agent": _ua,
            "Referer": "https://mediasetinfinity.mediaset.it/",
            "Origin": "https://mediasetinfinity.mediaset.it",
        },
        "body_sample": (_r.text or "")[:500],
    }
    print("[STREAM_PROBE] anonymous_login=", {k: v for k, v in out.items() if k != "headers"}, flush=True)
    return out
def _mediaset_call_playback_check_from_probe(probe_data: dict) -> dict:
    import requests as _requests

    _h = dict((probe_data or {}).get("headers") or {})
    _sid = _h.get("x-m-sid")
    _raw = _h.get("Authorization")

    if not (_sid and _raw):
        try:
            _login = _mediaset_anonymous_login_headers(_h.get("User-Agent", "Mozilla/5.0"))
            _login_headers = dict((_login or {}).get("headers") or {})
            if _login_headers:
                _h = {**_login_headers, **_h}
                _sid = _h.get("x-m-sid")
                _raw = _h.get("Authorization")
        except Exception as e:
            print("[STREAM_PROBE] anonymous_login merge error=", e, flush=True)

    if not (_sid and _raw):
        try:
            import json as _json
            _ch_file = "/home/sibilla-cumana/gatto/cat/plugins/stream_scraper/channels_out.json"
            _payload = _json.loads(Path(_ch_file).read_text(encoding="utf-8"))
            _ch = next((x for x in _payload.get("channels", []) if str(x.get("id", "")).lower() == "canale5"), None)
            _fallback_headers = dict((_ch or {}).get("headers") or {})
            if _fallback_headers:
                _h = {**_fallback_headers, **_h}
                _sid = _h.get("x-m-sid")
                _raw = _h.get("Authorization")
        except Exception:
            pass

    if not (_sid and _raw):
        print("[STREAM_PROBE] playback_check=", {"ok": False, "stage": "playback_check", "error": "missing_sid_or_authorization", "has_sid": bool(_sid), "has_auth": bool(_raw)}, flush=True)
        return {
            "ok": False,
            "stage": "playback_check",
            "error": "missing_sid_or_authorization",
        }

    _auth = _raw if str(_raw).lower().startswith("bearer ") else f"Bearer {_raw}"
    _pc_url = f"https://api-ott-prod-fe.mediaset.net/PROD/play/playback/check/v2.0?sid={_sid}"

    _pc = _requests.post(
        _pc_url,
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "origin": "https://mediasetinfinity.mediaset.it",
            "referer": "https://mediasetinfinity.mediaset.it/",
            "user-agent": _h.get("User-Agent", "Mozilla/5.0"),
            "sec-ch-ua": _h.get("sec-ch-ua", '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"'),
            "sec-ch-ua-mobile": _h.get("sec-ch-ua-mobile", "?0"),
            "sec-ch-ua-platform": _h.get("sec-ch-ua-platform", '"Linux"'),
            "authorization": _auth,
            "x-m-platform": _h.get("x-m-platform", "WEB"),
            "x-m-property": _h.get("x-m-property", "MPLAY"),
            "x-m-app-version": _h.get("x-m-app-version", "1.1.1"),
            "x-m-device-id": _h.get("x-m-device-id", ""),
            "x-m-sid": _sid,
            "x-m-user-context": _h.get("x-m-user-context", ""),
        },
        json={
            "channelCode": probe_data.get("callSign") or channel_id,
            "streamType": "LIVE",
            "delivery": "Streaming",
            "createDevice": True,
            "overrideAppName": "web//mediasetplay-web/1.1.0-509a584",
        },
        timeout=20,
    )

    out = {
        "ok": _pc.status_code == 200,
        "stage": "playback_check",
        "status_code": _pc.status_code,
        "playback_check_url": _pc_url,
        "body_sample": (_pc.text or "")[:500],
    }

    if _pc.status_code == 200:
        try:
            _j = _pc.json()
        except Exception:
            _j = {}
        _ms = (((_j or {}).get("response") or {}).get("mediaSelector") or {})
        out["mediaSelector"] = _ms

    print("[STREAM_PROBE] playback_check=", {"status_code": out["status_code"], "body_sample": out["body_sample"]}, flush=True)
    return out


def _mediaset_expand_media_selector_from_probe(playback_data: dict, probe_data: dict) -> dict:
    import requests as _requests

    _ms = dict((playback_data or {}).get("mediaSelector") or {})
    if not _ms.get("url"):
        return {
            "ok": False,
            "stage": "media_selector_expand",
            "error": "missing_media_selector_url",
        }

    _h = dict((probe_data or {}).get("headers") or {})
    _sm = _requests.get(
        _ms["url"],
        params={k: _ms.get(k) for k in ["formats", "assetTypes", "format", "balance", "auto", "tracking", "delivery"] if _ms.get(k) is not None},
        headers={
            "accept": "*/*",
            "origin": "https://mediasetinfinity.mediaset.it",
            "referer": "https://mediasetinfinity.mediaset.it/",
            "user-agent": _h.get("User-Agent", "Mozilla/5.0"),
        },
        timeout=20,
    )

    _txt = _sm.text or ""
    _m = _re.search(r"https?://[^\s\"'<>]+manifest_hr\.mpd", _txt, _re.I)
    _drm = _m.group(0) if _m else None

    print("[STREAM_PROBE] media_selector_expand=", {"smil_url": _sm.url, "drm_manifest_url": _drm}, flush=True)

    if not _drm:
        return {
            "ok": False,
            "stage": "media_selector_expand",
            "smil_url": _sm.url,
            "error": "missing_drm_manifest",
            "body_sample": _txt[:500],
        }

    _m2 = _re.search(r"https://live\d+p?-col\.msf\.cdn\.mediaset\.net/live/ch-([a-z0-9]+)/([a-z0-9]+)-dash-widevine\.isml/manifest_hr\.mpd", _drm, _re.I)
    if not _m2:
        return {
            "ok": False,
            "stage": "media_selector_expand",
            "smil_url": _sm.url,
            "drm_manifest_url": _drm,
            "error": "unexpected_drm_manifest_pattern",
        }

    _code = _m2.group(2).lower()
    _clear = f"https://live03-col.msf.cdn.mediaset.net/live/ch-{_code}/{_code}-clr.isml/manifest.mpd"
    _vr = _requests.get(_clear, timeout=20)

    return {
        "ok": _vr.status_code == 200 and "<MPD" in (_vr.text or ""),
        "stage": "media_selector_expand",
        "playback_check_url": playback_data.get("playback_check_url"),
        "media_selector_url": _ms.get("url"),
        "drm_manifest_url": _drm,
        "stream_url": _clear,
        "headers": {},
    }



def _mediaset_default_source_page(channel_id: str) -> str:
    ch = str(channel_id or "").strip().lower()
    mapping = {
        "canale5": "https://mediasetinfinity.mediaset.it/diretta/canale5_cC5",
        "italia1": "https://mediasetinfinity.mediaset.it/diretta/italia1_cI1",
        "rete4": "https://mediasetinfinity.mediaset.it/diretta/rete4_cR4",
        "tgcom24": "https://mediasetinfinity.mediaset.it/diretta/tgcom24_cKF",
        "topcrime": "https://mediasetinfinity.mediaset.it/diretta/topcrime_cLT",
        "20": "https://mediasetinfinity.mediaset.it/diretta/20mediaset_cLB",
        "iris": "https://mediasetinfinity.mediaset.it/diretta/iris_cKI",
        "la5": "https://mediasetinfinity.mediaset.it/diretta/la5_cKA",
        "extra": "https://mediasetinfinity.mediaset.it/diretta/mediasetextra_cKQ",
        "focus": "https://mediasetinfinity.mediaset.it/diretta/focus_cFU",
        "cine34": "https://mediasetinfinity.mediaset.it/diretta/cine34_cB6",
        "27": "https://mediasetinfinity.mediaset.it/diretta/twentyseven_cTS",
    }
    return mapping.get(ch, "https://mediasetinfinity.mediaset.it/diretta/canale5_cC5")

def _probe_mediaset_with_fallback(channel_id: str, source_page: str) -> dict:
    if str(channel_id).lower() == "canale5":
        api_graph = _probe_mediaset_api_graph_capture(channel_id)
        print("[STREAM_PROBE] api_graph_capture=", api_graph, flush=True)
        if api_graph.get("ok"):
            api_graph["previous_error"] = "html_probe_skipped_for_canale5"
            api_graph["source_page"] = source_page
            return api_graph

    res = _probe_mediaset_from_source_page(channel_id, source_page)
    if res.get("ok"):
        return res

    print("[STREAM_PROBE] source_page_probe_failed=", res, flush=True)

    api_graph = _probe_mediaset_api_graph_capture(channel_id)
    print("[STREAM_PROBE] api_graph_capture=", api_graph, flush=True)

    if api_graph.get("ok"):
        api_graph["previous_error"] = res.get("error")
        api_graph["source_page"] = source_page
        api_graph["probe_result"] = res
        return api_graph

    fallback = _legacy_channels_out_fallback(channel_id)
    fallback["previous_error"] = res.get("error")
    fallback["source_page"] = source_page
    fallback["probe_result"] = res
    fallback["api_graph_result"] = api_graph
    return fallback





def _skill_cache_dir():
    from pathlib import Path
    return Path("/home/sibilla-cumana/ralfloop_agent_scaffold/openshell_backend/cache_skills")

def _run_python_inline(code: str) -> str | None:
    import subprocess, tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8") as f:
        f.write(code + "\n")
        path = f.name
    try:
        r = subprocess.run(
            ["python3", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if out:
            return out.splitlines()[0].strip()
        if err:
            return None
        return None
    finally:
        try:
            os.unlink(path)
        except Exception:
            pass

def _skill_cache_answer(user_goal: str):
    import json
    import re
    from pathlib import Path

    q = (user_goal or "").strip().lower()
    base = _skill_cache_dir()
    if not base.exists():
        return None

    for path in sorted(base.rglob("*.json")):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            pat = obj.get("match", {}).get("regex")
            if not pat:
                continue
            m = re.search(pat, q)
            if not m:
                continue
            names = obj.get("input_names", [])
            vals = list(m.groups())
            if len(names) != len(vals):
                continue
            data = dict(zip(names, vals))
            template = obj.get("template") or ""
            code = template.format(**data)
            executor = obj.get("executor")
            if executor == "python_inline":
                return _run_python_inline(code)
        except Exception:
            continue

    return None

def _fast_math_answer(user_goal: str):
    import re

    q = (user_goal or "").strip().lower()

    def _num(x: str) -> float:
        return float(x.replace(",", "."))

    try:
        m = re.search(r'radice cubica di\s+(-?\d+(?:[.,]\d+)?)', q)
        if m:
            n = _num(m.group(1))
            x = n ** (1/3) if n >= 0 else -((-n) ** (1/3))
            return str(round(x, 6))

        m = re.search(r'radice quadrata di\s+(-?\d+(?:[.,]\d+)?)', q)
        if m:
            n = _num(m.group(1))
            if n < 0:
                return None
            return str(round(n ** 0.5, 6))

        m = re.search(r'(?:quanto fa|calcola)\s+(-?\d+(?:[.,]\d+)?)\s*([\+\-\*x/])\s*(-?\d+(?:[.,]\d+)?)', q)
        if m:
            a = _num(m.group(1))
            op = m.group(2)
            b = _num(m.group(3))
            if op == '+':
                return str(a + b)
            if op == '-':
                return str(a - b)
            if op in ('*', 'x'):
                return str(a * b)
            if op == '/':
                if b == 0:
                    return None
                return str(a / b)
    except Exception:
        return None

    return None
@app.post("/tasks/run")
def run_task(req: TaskRunRequest):
    import json as _json
    import re as _re

    common_result = route_common(req.user_goal or "", req.skill_context or "")
    if common_result is not None:
        return common_result

    low_goal = (req.user_goal or "").lower()
    low_skill = (req.skill_context or "").lower()
    selection = _task_run_selection(req)

    explicit_stream_request = (
        "stream probe" in low_goal
        or "trova lo stream" in low_goal
        or "url stream" in low_goal
        or "playlist m3u" in low_goal
        or "m3u8" in low_goal
        or "manifest" in low_goal
        or '"channel_id"' in (req.skill_context or "")
        or '"source_page"' in (req.skill_context or "")
    )

    if explicit_stream_request:
        import requests as _requests

        channel_id = "Canale5"
        source_page = ""

        try:
            skill = req.skill_context or ""
            skill_obj = _json.loads(skill) if skill else {}
            if isinstance(skill_obj, dict):
                channel_id = skill_obj.get("channel_id") or channel_id
                source_page = skill_obj.get("source_page") or source_page
        except Exception:
            try:
                skill = req.skill_context or ""
                m = _re.search(r'"channel_id"\s*:\s*"([^"]+)"', skill)
                if m:
                    channel_id = m.group(1)
                m = _re.search(r'"source_page"\s*:\s*"([^"]+)"', skill)
                if m:
                    source_page = m.group(1)
            except Exception:
                pass

        data = {"ok": False, "stream_url": None, "headers": {}}

        data = {"ok": False, "stream_url": None, "headers": {}}

        probe_data = {"ok": False}
        try:
            probe_data = _probe_mediaset_with_fallback(channel_id, source_page)
            print("[STREAM_PROBE] autonomous_probe=", probe_data, flush=True)
            if not probe_data.get("stream_url") and str(channel_id).lower() == "canale5":
                playback_data = _mediaset_call_playback_check_from_probe(probe_data)
                if playback_data.get("ok"):
                    expanded_data = _mediaset_expand_media_selector_from_probe(playback_data, probe_data)
                    if expanded_data.get("ok") and expanded_data.get("stream_url"):
                        probe_data = {**probe_data, **expanded_data}
            if probe_data.get("ok") and probe_data.get("stream_url"):
                data = {
                    "ok": True,
                    "stream_url": probe_data.get("stream_url"),
                    "headers": probe_data.get("headers", {}) or {},
                }
                print("[STREAM_PROBE] autonomous probe hit for", channel_id, flush=True)
        except Exception as e:
            print("[STREAM_PROBE] autonomous_probe error:", e, flush=True)

        if not data.get("ok"):
            try:
                import sys
                import types
                from pathlib import Path as _Path

                cat_root = _Path("/home/sibilla-cumana/gatto")
                plugin_root = _Path("/home/sibilla-cumana/gatto/cat/plugins/stream_scraper")
                if str(cat_root) not in sys.path:
                    sys.path.insert(0, str(cat_root))
                if str(plugin_root) not in sys.path:
                    sys.path.insert(0, str(plugin_root))

                if "cat.mad_hatter.decorators" not in sys.modules:
                    _cat = sys.modules.setdefault("cat", types.ModuleType("cat"))
                    _mh = sys.modules.setdefault("cat.mad_hatter", types.ModuleType("cat.mad_hatter"))
                    _dec = types.ModuleType("cat.mad_hatter.decorators")

                    def _noop_decorator(*args, **kwargs):
                        def _wrap(fn):
                            return fn
                        return _wrap

                    _dec.tool = _noop_decorator
                    _dec.hook = _noop_decorator
                    sys.modules["cat.mad_hatter.decorators"] = _dec

                from main import (
                    get_sniffer_driver,
                    sniff_video_url,
                    _mediaset_file_url_to_manifest,
                    _mediaset_file_url_to_remote_base,
                )
                from mediaset_resolver import mediaset_headers

                sniff_source_page = _mediaset_default_source_page(channel_id)
                sniff_channel_name = channel_id or "Canale5"

                driver = None
                sniff_url = None
                sniff_cookies = None
                sniff_ua = None

                try:
                    if "mediaset" in (sniff_source_page or "").lower():
                        sniff_url, sniff_cookies, sniff_ua = sniff_video_url(None, sniff_source_page, sniff_channel_name, channel_id=channel_id)
                    else:
                        driver = get_sniffer_driver()
                        sniff_url, sniff_cookies, sniff_ua = sniff_video_url(driver, sniff_source_page, sniff_channel_name, channel_id=channel_id)
                finally:
                    try:
                        if driver:
                            driver.quit()
                    except Exception:
                        pass

                if sniff_url:
                    try:
                        sniff_url = _mediaset_file_url_to_manifest(sniff_url)
                    except Exception:
                        pass
                    try:
                        sniff_url = _mediaset_file_url_to_remote_base(sniff_url)
                    except Exception:
                        pass

                    sniff_headers = {}
                    try:
                        sniff_headers.update(mediaset_headers(sniff_cookies, sniff_ua))
                    except Exception:
                        if sniff_ua:
                            sniff_headers["User-Agent"] = sniff_ua
                        if sniff_cookies:
                            sniff_headers["Cookie"] = sniff_cookies

                    data = {
                        "ok": True,
                        "stream_url": sniff_url,
                        "headers": sniff_headers,
                    }
                    print("[STREAM_PROBE] browser token flow hit for", channel_id, flush=True)

                    final_answer = _json.dumps({
                        "stream_url": data["stream_url"],
                        "headers": data.get("headers", {}),
                    }, ensure_ascii=False)

                    from fastapi.responses import JSONResponse as _JSONResponse
                    _resp_payload = {
                        "ok": True,
                        "mode": req.mode,
                        "used_profiles": {
                            "planner": selection["profiles"]["planner"],
                            "coder": selection["profiles"]["coder"],
                            "judge": selection["profiles"]["judge"],
                        },
                        "used_models": {
                            "planner": selection["models"]["planner"],
                            "coder": selection["models"]["coder"],
                            "judge": selection["models"]["judge"],
                        },
                        "used_rag": {
                            "planner": selection["rag"]["planner"],
                            "coder": selection["rag"]["coder"],
                            "judge": selection["rag"]["judge"],
                        },
                        "current_role": "stream_probe_bridge",
                        "role_history": ["stream_probe_bridge"],
                        "stop_reason": "goal_completed",
                        "final_answer": final_answer,
                        "artifacts": [],
                        "audit_summary": ["bridge::stream_scraper::browser_token_flow"],
                    }
                    _resp = _JSONResponse(content=_resp_payload)
                    return _resp
            except Exception as e:
                print("[STREAM_PROBE] browser token flow error:", e, flush=True)

        if not data.get("ok") and not probe_data.get("ok"):
            try:
                ch_file = "/home/sibilla-cumana/gatto/cat/plugins/stream_scraper/channels_out.json"
                payload = _json.loads(Path(ch_file).read_text(encoding="utf-8"))
                ch = next((x for x in payload.get("channels", []) if str(x.get("id","")).lower() == channel_id.lower()), None)
                if ch and ch.get("stream_url"):
                    data = {
                        "ok": True,
                        "stream_url": ch.get("stream_url"),
                        "headers": ch.get("headers", {}) or {},
                    }
                    print("[STREAM_PROBE] legacy fallback hit for", channel_id, flush=True)
            except Exception as e:
                print("[STREAM_BRIDGE] channels_out read error:", e, flush=True)

        if data.get("ok") and data.get("stream_url"):
            final_answer = _json.dumps({
                "stream_url": data["stream_url"],
                "headers": data.get("headers", {}),
            }, ensure_ascii=False)

            from fastapi.responses import JSONResponse as _JSONResponse
            _resp_payload = {
                "ok": True,
                "mode": req.mode,
                "used_profiles": {
                    "planner": selection["profiles"]["planner"],
                    "coder": selection["profiles"]["coder"],
                    "judge": selection["profiles"]["judge"],
                },
                "used_models": {
                    "planner": selection["models"]["planner"],
                    "coder": selection["models"]["coder"],
                    "judge": selection["models"]["judge"],
                },
                "used_rag": {
                    "planner": selection["rag"]["planner"],
                    "coder": selection["rag"]["coder"],
                    "judge": selection["rag"]["judge"],
                },
                "current_role": "stream_probe_bridge",
                "role_history": ["stream_probe_bridge"],
                "stop_reason": "goal_completed",
                "final_answer": final_answer,
                "artifacts": [],
                "audit_summary": ["bridge::stream_scraper::probe_endpoint"],
            }
            _resp = _JSONResponse(content=_resp_payload)
            return _resp

        if probe_data.get("ok") and not probe_data.get("stream_url"):
            final_answer = _json.dumps({
                "probe_stage": probe_data.get("stage"),
                "channel_id": probe_data.get("channel_id"),
                "callSign": probe_data.get("callSign"),
                "cardTitle": probe_data.get("cardTitle"),
                "guid": probe_data.get("guid"),
                "title": probe_data.get("title"),
                "liveAllowed": probe_data.get("liveAllowed"),
                "restartAllowed": probe_data.get("restartAllowed"),
                "startTime": probe_data.get("startTime"),
                "endTime": probe_data.get("endTime"),
                "api_graph_url": probe_data.get("api_graph_url"),
            }, ensure_ascii=False)

            return {
                "ok": True,
                "mode": req.mode,
                "used_profiles": {
                    "planner": selection["profiles"]["planner"],
                    "coder": selection["profiles"]["coder"],
                    "judge": selection["profiles"]["judge"],
                },
                "used_models": {
                    "planner": selection["models"]["planner"],
                    "coder": selection["models"]["coder"],
                    "judge": selection["models"]["judge"],
                },
                "used_rag": {
                    "planner": selection["rag"]["planner"],
                    "coder": selection["rag"]["coder"],
                    "judge": selection["rag"]["judge"],
                },
                "current_role": "stream_probe_bridge",
                "role_history": ["stream_probe_bridge"],
                "stop_reason": "goal_completed_partial",
                "final_answer": final_answer,
                "artifacts": [],
                "audit_summary": ["bridge::stream_scraper::api_graph_capture"],
            }

        return {
            "ok": False,
            "mode": req.mode,
            "used_profiles": {
                "planner": selection["profiles"]["planner"],
                "coder": selection["profiles"]["coder"],
                "judge": selection["profiles"]["judge"],
            },
            "used_models": {
                "planner": selection["models"]["planner"],
                "coder": selection["models"]["coder"],
                "judge": selection["models"]["judge"],
            },
            "used_rag": {
                "planner": selection["rag"]["planner"],
                "coder": selection["rag"]["coder"],
                "judge": selection["rag"]["judge"],
            },
            "current_role": "stream_probe_bridge",
            "role_history": ["stream_probe_bridge"],
            "stop_reason": "bridge_failed",
            "final_answer": '{"stream_url":null}',
            "artifacts": [],
            "audit_summary": ["bridge::stream_scraper::probe_endpoint::failed"],
        }

    from ralfloop_agent.core.loop import RalfloopAgent
    from ralfloop_agent.core.state import AgentState
    from ralfloop_agent.adapters.openshell_real_adapter import OpenShellAdapterReal
    from ralfloop_agent.logging.audit import AuditLogger
    from ralfloop_agent.providers.ollama import OllamaPlanner, DeterministicPlanner

    adapter = OpenShellAdapterReal(base_url="http://127.0.0.1:19090")

    planner = OllamaPlanner(
        base_url="http://127.0.0.1:11434",
        model=selection["models"]["planner"],
        fallback=DeterministicPlanner(),
        timeout_sec=60,
    )
    planner.role_name = "planner"
    planner.internal_prompt_style = selection["internal_prompt_style"]
    coder_planner = OllamaPlanner(
        base_url="http://127.0.0.1:11434",
        model=selection["models"]["coder"],
        fallback=DeterministicPlanner(),
        timeout_sec=60,
    )
    coder_planner.role_name = "coder"
    coder_planner.internal_prompt_style = selection["internal_prompt_style"]
    judge_planner = OllamaPlanner(
        base_url="http://127.0.0.1:11434",
        model=selection["models"]["judge"],
        fallback=DeterministicPlanner(),
        timeout_sec=60,
    )
    judge_planner.role_name = "judge"
    judge_planner.internal_prompt_style = selection["internal_prompt_style"]

    logger = AuditLogger(store_path="./logs")

    agent = RalfloopAgent(
        adapter=adapter,
        planner=planner,
        coder_planner=coder_planner,
        judge_planner=judge_planner,
        logger=logger,
    )

    direct_skill = try_direct_skill(req.user_goal or "")
    if direct_skill:
        skill_name, skill_answer = direct_skill
        validation = validate_skill_output(skill_name, skill_answer)

        if is_skill_output_sufficient(skill_name, skill_answer):
            return {
                "ok": True,
                "mode": f"skill::{skill_name}",
                "used_profiles": {},
                "used_models": {},
                "used_rag": {},
                "current_role": f"skill::{skill_name}",
                "role_history": [f"skill::{skill_name}"],
                "stop_reason": "goal_completed",
                "final_answer": skill_answer,
                "artifacts": [],
                "audit_summary": [f"fastpath::skill::{skill_name}"],
                "autofix_candidate": {},
            }

        payload = build_skill_insufficient_response(
            skill_name=skill_name,
            user_goal=req.user_goal or "",
            final_answer=skill_answer,
            validation=validation,
            include_runtime_fields=True,
        )
        payload = maybe_attach_coder_text(
            payload=payload,
            model_name=selection["models"]["coder"],
            base_url="http://127.0.0.1:11434",
            timeout_sec=60,
        )
        af = dict(payload.get("autofix_candidate", {}) or {})
        validation_attempt = maybe_apply_and_validate_coder_patch(
            repo_root="/home/sibilla-cumana/ralfloop_agent_scaffold",
            autofix_candidate=af,
            timeout_sec=120,
        )
        if validation_attempt:
            af["coder_validation_attempt"] = validation_attempt
            if validation_attempt.get("ok"):
                apply_attempt = apply_coder_patch_permanently(
                    repo_root="/home/sibilla-cumana/ralfloop_agent_scaffold",
                    autofix_candidate=af,
                )
                if apply_attempt:
                    af["coder_apply_attempt"] = apply_attempt

                    rerun_skill = try_direct_skill(req.user_goal or "")
                    rerun_validation = None
                    rerun_answer = None
                    rerun_ok = False

                    if rerun_skill:
                        rerun_skill_name, rerun_answer = rerun_skill
                        rerun_validation = validate_skill_output(rerun_skill_name, rerun_answer)
                        rerun_ok = is_skill_output_sufficient(rerun_skill_name, rerun_answer)

                    af["post_apply_rerun"] = {
                        "ok": rerun_ok,
                        "skill_name": rerun_skill[0] if rerun_skill else "",
                        "final_answer": rerun_answer or "",
                        "validation": rerun_validation or {},
                    }

                    if rerun_ok:
                        payload = {
                            "ok": True,
                            "mode": f"skill::{rerun_skill[0]}",
                            "used_profiles": {},
                            "used_models": {},
                            "used_rag": {},
                            "current_role": f"skill::{rerun_skill[0]}",
                            "role_history": [f"skill::{rerun_skill[0]}"],
                            "stop_reason": "goal_completed_after_autofix",
                            "final_answer": rerun_answer or "",
                            "artifacts": [],
                            "audit_summary": [
                                f"fastpath::skill::{rerun_skill[0]}",
                                "autofix::coder_patch_applied",
                                "autofix::post_apply_rerun_ok",
                            ],
                            "autofix_candidate": af,
                        }
                        return payload
            payload["autofix_candidate"] = af
        return payload

    state = agent.run(
        user_goal=req.user_goal,
        constraints=[],
        context={
            "skill_context": req.skill_context or "",
            "extra_context": req.extra_context or {},
            "planner_model_profile": selection["profiles"]["planner"],
            "coder_model_profile": selection["profiles"]["coder"],
            "judge_model_profile": selection["profiles"]["judge"],
            "planner_model_name": selection["models"]["planner"],
            "coder_model_name": selection["models"]["coder"],
            "judge_model_name": selection["models"]["judge"],
            "planner_rag_collection": selection["rag"]["planner"],
            "coder_rag_collection": selection["rag"]["coder"],
            "judge_rag_collection": selection["rag"]["judge"],
            "internal_prompt_style": selection["internal_prompt_style"],
        },
    )
    try:
        print("[TASKS_RUN] stop_reason=", state.stop_reason, flush=True)
        print("[TASKS_RUN] current_role=", state.current_role, flush=True)
        print("[TASKS_RUN] consecutive_failures=", state.consecutive_failures, flush=True)
        print("[TASKS_RUN] last_action=", getattr(state, "last_action", None), flush=True)
        print("[TASKS_RUN] last_result=", getattr(state, "last_result", None), flush=True)
    except Exception as e:
        print("[TASKS_RUN] debug_print_error=", e, flush=True)

    state.planner_model_profile = selection["profiles"]["planner"]
    state.coder_model_profile = selection["profiles"]["coder"]
    state.judge_model_profile = selection["profiles"]["judge"]
    state.planner_model_name = selection["models"]["planner"]
    state.coder_model_name = selection["models"]["coder"]
    state.judge_model_name = selection["models"]["judge"]
    state.planner_rag_collection = selection["rag"]["planner"]
    state.coder_rag_collection = selection["rag"]["coder"]
    state.judge_rag_collection = selection["rag"]["judge"]

    promoted_skill_path = maybe_autopromote_candidate(
        req.user_goal or "",
        state.final_answer,
        planner_text if "planner_text" in locals() else "",
        coder_text if "coder_text" in locals() else "",
    )

    state_data = state.model_dump() if hasattr(state, "model_dump") else dict(getattr(state, "__dict__", {}) or {})
    runtime_debug = list((state_data.get("context", {}) or {}).get("runtime_debug", []) or [])
    payload = {
        "ok": state_data.get("status") == "completed",
        "mode": str(state_data.get("mode", "") or ""),
        "used_profiles": dict(selection["profiles"]),
        "used_models": dict(selection["models"]),
        "used_rag": dict(selection["rag"]),
        "used_internal_prompt_style": str(selection["internal_prompt_style"]),
        "current_role": str(state_data.get("current_role", "") or ""),
        "role_history": list(state_data.get("role_history", []) or []),
        "stop_reason": str(state_data.get("stop_reason", "") or ""),
        "final_answer": str(state_data.get("final_answer", "") or ""),
        "artifacts": list(state_data.get("artifacts", []) or []),
        "audit_summary": list(state_data.get("audit_summary", []) or []),
        "autofix_candidate": dict(state_data.get("autofix_candidate", {}) or {}),
    }
    if not payload["ok"]:
        payload["debug_runtime"] = runtime_debug
    return payload
