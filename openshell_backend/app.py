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

from ralfloop_agent.shell_judge import (
    RalfShellJudge, ShellDecision, ShellPolicy, safe_execution_environment,
)

BASE_DIR = Path(os.environ.get("RALF_OPEN_SHELL_STATE_DIR", "/home/sibilla-cumana/ralfloop_agent_scaffold/.openshell_backend")).expanduser()
BASE_DIR.mkdir(parents=True, exist_ok=True)

AUDIT_LOG = BASE_DIR / "audit.jsonl"

app = FastAPI(title="Ralfloop OpenShell Backend")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.on_event("shutdown")
def _shutdown_managed_llama_cpp_children() -> None:
    from ralfloop_agent.providers.llama_cpp_server import _reap_children_at_exit

    _reap_children_at_exit()

try:
    from openshell_backend.atm_telegram import router as atm_telegram_router

    app.include_router(atm_telegram_router)
except Exception as exc:
    audit("atm_telegram_router_load_failed", error=repr(exc))

BACKEND_VENV_BIN = os.path.expanduser(os.environ.get("RALF_BACKEND_VENV_BIN", "~/ralfloop_agent_scaffold/.venv/bin"))
SELF_BASE_URL = os.environ.get("RALF_OPEN_SHELL_BASE_URL", "http://127.0.0.1:19090").rstrip("/")


def audit(event: str, **fields) -> None:
    row = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        **fields,
    }
    with AUDIT_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


try:
    from ralfloop_agent.domains.telegram_approval_api import register_domain_approval_routes

    register_domain_approval_routes(app)
except Exception as exc:
    audit("domain_approval_routes_load_failed", error=repr(exc))

try:
    from ralfloop_agent.integration.local_maintenance import register_local_maintenance_routes

    register_local_maintenance_routes(app)
except Exception as exc:
    audit("local_maintenance_routes_load_failed", error=repr(exc))

try:
    from ralfloop_agent.integration.eyf_support4youth_cdp import (
        register_eyf_support4youth_routes,
    )

    register_eyf_support4youth_routes(app)
except Exception as exc:
    audit("eyf_support4youth_routes_load_failed", error=repr(exc))

try:
    from ralfloop_agent.repair.approval import register_repair_approval_routes

    register_repair_approval_routes(app)
except Exception as exc:
    audit("repair_approval_routes_load_failed", error=repr(exc))

try:
    from openshell_backend.chat_api import router as chat_router

    app.include_router(chat_router)
except Exception as exc:
    audit("chat_router_load_failed", error=repr(exc))

try:
    from openshell_backend.assistant_v1_api import router as assistant_v1_router

    app.include_router(assistant_v1_router)
except Exception as exc:
    audit("assistant_v1_router_load_failed", error=repr(exc))


def _read_arci_profile(context_factory=None) -> dict[str, object]:
    """Run fixed semantic ARCI read; never expose transport error details."""
    if context_factory is None:
        from src.arci import ArciMCPContext

        context_factory = ArciMCPContext.from_environment
    try:
        with context_factory() as gateway:
            return dict(gateway.read_organization_profile())
    except Exception as exc:
        try:
            audit("arci_profile_read_failed", error_type=type(exc).__name__)
        except Exception:
            pass
        return {
            "ok": False,
            "operation": "read_organization_profile",
            "status": "SOURCE_UNAVAILABLE",
            "session_authenticated": False,
            "read_operations": [],
            "write_operations": 0,
            "side_effects": 0,
            "writes": 0,
            "sends": 0,
        }


_ARCI_READERS = {
    "club": ("read_club", "read_club"),
    "cards": ("read_current_cards", "read_current_cards"),
    "committee": ("read_committee", "read_committee"),
    "regional": ("read_regional", "read_regional"),
    "dashboard-alerts": ("read_dashboard_alerts", "read_dashboard_alerts"),
}


def _read_arci_resource(resource: str, context_factory=None) -> dict[str, object]:
    if resource not in _ARCI_READERS:
        return {"ok": False, "status": "NOT_FOUND", "side_effects": 0, "writes": 0, "sends": 0}
    method_name, operation = _ARCI_READERS[resource]
    if context_factory is None:
        from src.arci import ArciMCPContext
        context_factory = ArciMCPContext.from_environment
    try:
        with context_factory() as gateway:
            return dict(getattr(gateway, method_name)())
    except Exception as exc:
        try:
            audit("arci_resource_read_failed", resource=resource, error_type=type(exc).__name__)
        except Exception:
            pass
        return {
            "ok": False, "operation": operation, "status": "SOURCE_UNAVAILABLE",
            "read_operations": [], "write_operations": 0, "side_effects": 0,
            "content_role": "data", "writes": 0, "sends": 0,
        }


@app.get("/portals/arci/profile")
def arci_profile() -> dict[str, object]:
    return _read_arci_profile()


@app.get("/portals/arci/{resource}")
def arci_read_resource(resource: str) -> dict[str, object]:
    return _read_arci_resource(resource)


def sandbox_root(sid: str) -> Path:
    return BASE_DIR / sid / "workspace"


def resolve_in_sandbox(root: Path, rel_path: str) -> Path:
    rel = rel_path.lstrip("/")
    full = (root / rel).resolve()
    if root.resolve() not in full.parents and full != root.resolve():
        raise HTTPException(status_code=403, detail="path_not_allowed")
    return full


def _safe_shell_environment() -> dict[str, str]:
    return safe_execution_environment(os.environ, path_prefix=BACKEND_VENV_BIN)


def review_shell_command(command: str, *, cwd: str | Path) -> object:
    return RalfShellJudge(ShellPolicy.for_sandbox(cwd)).review(
        command, str(cwd), _safe_shell_environment(),
    )


def check_command_allowed(command: str, cwd: str | Path | None = None) -> tuple[bool, str]:
    """Compatibility wrapper; all decisions delegate to the canonical judge."""
    target = Path(cwd or BASE_DIR)
    target.mkdir(parents=True, exist_ok=True)
    result = review_shell_command(command, cwd=target)
    return (
        result.decision in {ShellDecision.ALLOW, ShellDecision.ALLOW_READONLY},
        result.deterministic_reason,
    )


def _shell_path_classes(root: Path, review: object) -> dict[str, int]:
    groups = {
        "read": review.capabilities.resolved_paths_read,
        "write": review.capabilities.resolved_paths_write,
        "delete": review.capabilities.resolved_paths_delete,
    }
    result: dict[str, int] = {}
    root = root.resolve(strict=True)
    protected = tuple(Path(value).resolve(strict=False) for value in (
        "/boot", "/etc", "/usr", "/var/lib", "/home/sibilla-cumana/ralfloop-production",
    ))
    secret = tuple(Path(value).resolve(strict=False) for value in (
        "/home/bandi/.config", "/home/sibilla-cumana/.config", "/run/credentials",
    ))
    for operation, paths in groups.items():
        for raw in paths:
            path = Path(raw)
            if path == root or root in path.parents:
                label = "sandbox"
            elif any(path == value or value in path.parents for value in secret):
                label = "secret"
            elif any(path == value or value in path.parents for value in protected):
                label = "protected"
            else:
                label = "external"
            key = f"{operation}:{label}"
            result[key] = result.get(key, 0) + 1
    return result


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

    review = review_shell_command(payload.command, cwd=root)
    allowed = review.decision in {ShellDecision.ALLOW, ShellDecision.ALLOW_READONLY}
    audit_fields = {
        "event_id": str(uuid.uuid4()),
        "sandbox_id": sid,
        "command_digest": RalfShellJudge.command_digest(payload.command),
        "decision": review.decision.value,
        "reason": review.deterministic_reason,
        "deterministic_reason": review.deterministic_reason,
        "destructive_level": review.capabilities.destructive_level.value,
        "read_paths": len(review.capabilities.resolved_paths_read),
        "write_paths": len(review.capabilities.resolved_paths_write),
        "delete_paths": len(review.capabilities.resolved_paths_delete),
        "path_classes": _shell_path_classes(root, review),
        "reviewer_used": False,
        "execution_allowed": allowed,
    }
    if not allowed:
        audit("exec_denied", **audit_fields)
        raise HTTPException(status_code=403, detail={
            "decision": review.decision.value,
            "reason": review.deterministic_reason,
            "reviewer_required": review.reviewer_required,
        })

    try:
        env = _safe_shell_environment()
        proc = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", payload.command],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=payload.timeout_sec,
            env=env,
        )
        audit(
            "exec",
            **audit_fields,
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
        audit("exec_timeout", **audit_fields, timeout_sec=payload.timeout_sec)
        return {
            "ok": False,
            "exit_code": 124,
            "stdout": e.stdout or "",
            "stderr": e.stderr or "command timed out",
        }
    except Exception as exc:
        audit("exec_exception", **audit_fields, error_class=type(exc).__name__)
        raise


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

class TaskRunRequest(BaseModel):
    user_goal: str
    mode: str = "planner_coder_judge"
    planner_model_profile: str = "generalist"
    coder_model_profile: str = "coder"
    judge_model_profile: str = "generalist"

    planner_model_name: str = "qwen2.5:7b"
    coder_model_name: str = "qwen2.5:7b"
    judge_model_name: str = "qwen2.5:7b"

    planner_rag_collection: str = "ralfloop_planner"
    coder_rag_collection: str = "ralfloop_coder"
    judge_rag_collection: str = "ralfloop_judge"

    skill_context: Optional[str] = None
    extra_context: Optional[Dict[str, Any]] = None



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

    html_unescaped = html.replace("\/", "/")
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


def _run_read_only_system_inspection(req: TaskRunRequest, route_model, capability_route: dict):
    from ralfloop_agent.integration.execution_provenance import provenance_from_results
    from ralfloop_agent.models.result_envelope import ResultEnvelope
    from ralfloop_agent.integration.system_inspection import (
        ReadOnlySystemExecutor,
        default_inspection_plan,
        execute_inspection_plan,
        format_inspection_answer,
        resolve_inspection_root,
    )

    terminal = dict((req.extra_context or {}).get("terminal_client") or {})
    provider = str(terminal.get("provider") or "") or None
    endpoint = str(terminal.get("provider_endpoint") or "") or None
    model_id = str(terminal.get("model_id") or "") or None
    root = resolve_inspection_root(req.extra_context)
    executor = ReadOnlySystemExecutor(root)
    results = execute_inspection_plan(executor, default_inspection_plan(root))
    for result in results:
        evidence = result.evidence
        audit(
            "read_only_inspection_command",
            result_id=result.result_id,
            command=evidence.command,
            path=evidence.path,
            exit_code=evidence.exit_code,
        )
    provenance = provenance_from_results(
        ((result.result_id, result.evidence) for result in results),
        provider=provider,
        endpoint=endpoint,
        model_id=model_id,
    )
    answer = format_inspection_answer(results)
    all_success = bool(results) and all(result.evidence.exit_code == 0 for result in results)
    envelope = ResultEnvelope(
        route=route_model,
        evidence=results[0].evidence if results else None,
        answer=answer,
        meta={
            "source": "deterministic_read_only_inspection",
            "mode": req.mode,
            "inspection_root": str(root),
            "interaction_mode": "agent",
            "capability": "read_only_system_inspection",
            "provider": provider,
            "endpoint": endpoint,
            "model_id": model_id,
        },
        provenance=provenance,
    )
    return {
        "ok": all_success,
        "mode": req.mode,
        "current_role": "read_only_system_inspection",
        "role_history": ["capability_router", "read_only_system_inspection"],
        "stop_reason": (
            "read_only_inspection_completed"
            if all_success
            else "read_only_inspection_partial"
        ),
        "capability_route": capability_route,
        "external_action": False,
        "approval_required": False,
        "interaction_mode": "agent",
        "capability": "read_only_system_inspection",
        "result_envelope": envelope.model_dump(mode="json"),
        "final_answer": answer,
        "artifacts": [],
        "audit_summary": [
            f"read_only_inspection::{result.result_id}::{result.evidence.exit_code}"
            for result in results
        ],
    }

def _local_code_workdir(req: TaskRunRequest) -> Path | None:
    terminal = dict((req.extra_context or {}).get("terminal_client") or {})
    raw = str(terminal.get("cwd") or "").strip()
    if not raw:
        return None
    requested = Path(raw).expanduser()
    if not requested.is_absolute() or not requested.is_dir():
        return None
    resolved = requested.resolve()
    roots = os.environ.get(
        "RALF_CODE_WORKTREE_ROOTS", "/home/bandi:/home/sibilla-cumana"
    ).split(":")
    allowed_roots = [Path(item).expanduser().resolve() for item in roots if item.strip()]
    if not any(resolved == root or resolved.is_relative_to(root) for root in allowed_roots):
        return None
    result = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        check=False, text=True, capture_output=True, timeout=5,
    )
    if result.returncode != 0:
        return None
    try:
        git_root = Path(result.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if git_root == resolved else None


def _run_local_code_patch(req: TaskRunRequest, route_model, capability_route: dict):
    from dataclasses import replace
    from ralfloop_agent.programmer import ProgrammerAgent, ProgrammerConfig, ProgrammerState

    workdir = _local_code_workdir(req)
    if workdir is None:
        return {
            "ok": False,
            "mode": req.mode,
            "current_role": "programmer",
            "role_history": ["capability_router", "programmer"],
            "stop_reason": "trusted_git_worktree_required",
            "interaction_mode": "agent",
            "capability": "local_code_patch",
            "approval_required": False,
            "capability_route": capability_route,
            "final_answer": "Programmatore non eseguito: serve un cwd che sia la radice di un worktree Git locale consentito.",
            "artifacts": [],
            "audit_summary": ["programmer::worktree_rejected"],
        }
    validator = str(
        ((req.extra_context or {}).get("terminal_client") or {}).get("validator_command")
        or "git diff --check"
    )
    programmer_config = ProgrammerConfig.from_environment(
        workdir=workdir,
        task=req.user_goal,
        validator_command=validator,
        allow_test_changes=False,
        allow_worker_shell=os.environ.get("RALF_CODE_WORKER_SHELL", "0").strip().lower()
        in {"1", "true", "yes", "on"},
    )
    if not programmer_config.allowed_roots:
        # `_local_code_workdir` has already checked the operational allowlist;
        # carry that trust boundary forward without inventing host-specific paths.
        programmer_config = replace(programmer_config, allowed_roots=(workdir.parent,))
    result = ProgrammerAgent(programmer_config).run()
    report = result.as_dict()
    passed = result.state is ProgrammerState.CANDIDATE_READY
    return {
        "ok": passed,
        "mode": req.mode,
        "current_role": "programmer",
        "role_history": ["capability_router", "programmer"],
        "stop_reason": "programmer_candidate_ready" if passed else f"programmer_{result.state.value}",
        "interaction_mode": "agent",
        "capability": "local_code_patch",
        "approval_required": False,
        "capability_route": capability_route,
        "final_answer": json.dumps(report, ensure_ascii=False, sort_keys=True),
        "artifacts": [],
        "audit_summary": [f"programmer::{result.reason}::{result.state.value}"],
    }


def _run_task_impl(req: TaskRunRequest):
    import json as _json
    import re as _re
    from ralfloop_agent.integration.capability_adapter import route_task, route_to_legacy_dict
    from ralfloop_agent.models.result_envelope import ResultEnvelope
    from src.skills import is_bandi_semantic_intent, is_local_maintenance_intent

    low_goal = (req.user_goal or "").lower()
    low_skill = (req.skill_context or "").lower()
    route_model = route_task(req.user_goal, req.mode)
    capability_route = route_to_legacy_dict(route_model)

    if req.mode == "route_only" or (req.extra_context or {}).get("route_only") is True:
        if (
            os.getenv("RALFLOOP_UNIFIED_ASSISTANT", "0") == "1"
            and (
                str((req.extra_context or {}).get("source") or "").startswith("telegram_")
                or str((req.extra_context or {}).get("source") or "") == "ralf_terminal"
            )
        ):
            try:
                from ralfloop_agent.unified_assistant.runtime import unified_route_probe

                unified_route = unified_route_probe(req.user_goal, req.extra_context or {})
                if unified_route is not None:
                    capability_route.update(unified_route)
            except Exception as exc:
                audit("unified_route_probe_failed_closed", error=type(exc).__name__)
        envelope = ResultEnvelope(
            route=route_model,
            answer="route_only",
            meta={"source": "capability_adapter", "mode": req.mode},
        )
        return {
            "ok": True,
            "mode": req.mode,
            "current_role": "capability_router",
            "role_history": ["capability_router"],
            "stop_reason": "route_only",
            "capability_route": capability_route,
            "result_envelope": envelope.model_dump(mode="json"),
            "final_answer": _json.dumps(capability_route, ensure_ascii=False, indent=2),
            "artifacts": [],
            "audit_summary": ["capability_router::route_only"],
        }

    # Meowgram remains the only Telegram entry point. Explicit legacy commands
    # are consumed by Meowgram first; this feature-flagged branch handles only
    # natural Telegram requests. Route-only probes above expose tool-backed mode
    # without executing providers, preserving Meowgram's two-step contract.
    unified_candidate = (
        os.getenv("RALFLOOP_UNIFIED_ASSISTANT", "0") == "1"
        and (
            str((req.extra_context or {}).get("source") or "").startswith("telegram_")
            or str((req.extra_context or {}).get("source") or "") == "ralf_terminal"
        )
    )
    if unified_candidate:
        try:
            from ralfloop_agent.unified_assistant.runtime import (
                is_unified_telegram_request,
                run_unified_telegram,
            )

            if is_unified_telegram_request(req.user_goal, req.extra_context or {}):
                return run_unified_telegram(req.user_goal, req.extra_context or {})
        except Exception as exc:
            audit("unified_assistant_failed_closed", error=type(exc).__name__)
            return {
            "ok": False,
            "mode": req.mode,
            "current_role": "unified_assistant",
            "role_history": ["capability_router", "unified_assistant"],
            "stop_reason": "unified_assistant_unavailable",
            "interaction_mode": "unified_assistant",
            "capability": "unified_assistant",
            "approval_required": False,
            "capability_route": capability_route,
            "final_answer": "Unified assistant non disponibile; nessuna azione eseguita.",
            "artifacts": [],
            "audit_summary": ["unified_assistant::failed_closed"],
            }

    if route_model.mode == "read_only_system_inspection":
        return _run_read_only_system_inspection(
            req, route_model, capability_route
        )

    if (
        route_model.mode == "patch_allowed"
        and is_local_maintenance_intent(req.user_goal)
    ):
        return _run_local_code_patch(req, route_model, capability_route)

    if (
        route_model.mode == "external_action"
        and "browser" in route_model.mcp_used
        and "bandi" in route_model.skills_used
        and is_bandi_semantic_intent(req.user_goal)
        and not is_local_maintenance_intent(req.user_goal)
    ):
        from ralfloop_agent.domains.bandi_runtime_context import load_bandi_runtime_context
        from ralfloop_agent.integration.execution_provenance import empty_execution_provenance

        bandi_context = load_bandi_runtime_context()
        action = {
            "action": "bandi_browser_fill",
            "connector": "browser",
            "status": "mcp_execution_required",
            "user_goal": req.user_goal,
            "call_id": (bandi_context.get("application_status") or {}).get("call_id"),
            "portal_draft_id": (bandi_context.get("application_status") or {}).get("portal_draft_id"),
            "application_status": (bandi_context.get("application_status") or {}).get("status"),
            "blocking_requirements": (bandi_context.get("application_status") or {}).get("blocking_requirements", []),
            "stop_rules": ["no_final_submission", "no_signature_fabrication", "no_unverified_declaration"],
        }
        terminal = dict((req.extra_context or {}).get("terminal_client") or {})
        provenance = empty_execution_provenance(
            provider=str(terminal.get("provider") or "") or None,
            endpoint=str(terminal.get("provider_endpoint") or "") or None,
            model_id=str(terminal.get("model_id") or "") or None,
        )
        envelope = ResultEnvelope(
            route=route_model,
            answer=_json.dumps(action, ensure_ascii=False, sort_keys=True),
            meta={
                "source": "ralfloop_agent",
                "interaction_mode": "agent",
                "capability": "bandi_browser_fill",
                "browser_action": action,
            },
            provenance=provenance,
        )
        return {
            "ok": True,
            "mode": req.mode,
            "current_role": "bandi_browser_orchestrator",
            "role_history": ["capability_router", "bandi_browser_orchestrator"],
            "stop_reason": "mcp_execution_required",
            "interaction_mode": "agent",
            "capability": "bandi_browser_fill",
            "approval_required": False,
            "capability_route": capability_route,
            "result_envelope": envelope.model_dump(mode="json"),
            "final_answer": envelope.answer,
            "artifacts": [],
            "audit_summary": ["bandi_browser_fill::mcp_execution_required"],
        }

    if is_local_maintenance_intent(req.user_goal) and route_model.mode == "external_action":
        from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
        from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
        from ralfloop_agent.integration.local_maintenance import (
            LocalMaintenanceApprovalService,
            canonical_action_for_goal,
        )

        action_id = canonical_action_for_goal(req.user_goal)
        if action_id is None:
            return {
                "ok": False,
                "mode": req.mode,
                "current_role": "local_software_maintenance",
                "role_history": ["capability_router", "local_software_maintenance"],
                "stop_reason": "unsupported_canonical_local_action",
                "interaction_mode": "agent",
                "capability": "local_software_maintenance",
                "approval_required": True,
                "capability_route": capability_route,
                "final_answer": "Azione locale non presente nel registro canonico; nessun comando eseguito.",
                "artifacts": [],
                "audit_summary": ["local_maintenance::unsupported_canonical_action"],
            }
        policy = DomainApprovalPolicy.from_env()
        service = LocalMaintenanceApprovalService(
            DomainApprovalStore(policy=policy), policy=policy
        )
        preview = service.preview(action_id)
        approval = service.request(action_id, requested_by="ralf_task_router")
        answer = {
            "action_id": action_id,
            "preview": preview,
            "approval": approval,
            "apply_endpoint": "/local-maintenance/requests/{request_id}/apply",
            "executed": False,
        }
        return {
            "ok": False,
            "mode": req.mode,
            "current_role": "local_software_maintenance",
            "role_history": ["capability_router", "local_software_maintenance"],
            "stop_reason": "local_maintenance_approval_required",
            "interaction_mode": "agent",
            "capability": "local_software_maintenance",
            "approval_required": True,
            "capability_route": capability_route,
            "local_maintenance": answer,
            "final_answer": _json.dumps(answer, ensure_ascii=False, sort_keys=True),
            "artifacts": [],
            "audit_summary": ["local_maintenance::previewed_and_bound"],
        }

    if route_model.requires_confirmation and not (req.extra_context or {}).get("human_confirmed"):
        from ralfloop_agent.integration.execution_provenance import empty_execution_provenance
        from ralfloop_agent.integration.confirmation_store import get_confirmation, request_confirmation
        from src.models import Evidence

        confirmation_id = request_confirmation(
            "external_action",
            {"user_goal": req.user_goal, "route": route_model.model_dump()},
        )
        confirmation = get_confirmation(confirmation_id)
        evidence = Evidence(command="mcp:external_action", path="external", exit_code=0)
        terminal = dict((req.extra_context or {}).get("terminal_client") or {})
        provenance = empty_execution_provenance(
            provider=str(terminal.get("provider") or "") or None,
            endpoint=str(terminal.get("provider_endpoint") or "") or None,
            model_id=str(terminal.get("model_id") or "") or None,
        )
        envelope = ResultEnvelope(
            route=route_model,
            evidence=evidence,
            confirmation=confirmation,
            answer="human_confirmation_required",
            meta={
                "source": "capability_adapter",
                "mode": req.mode,
                "interaction_mode": "agent",
                "capability": "protected_external_action",
            },
            provenance=provenance,
        )
        return {
            "ok": False,
            "mode": req.mode,
            "current_role": "capability_router",
            "role_history": ["capability_router"],
            "stop_reason": "human_confirmation_required",
            "interaction_mode": "agent",
            "capability": "protected_external_action",
            "approval_required": True,
            "capability_route": capability_route,
            "result_envelope": envelope.model_dump(mode="json"),
            "pending_confirmation_id": confirmation_id,
            "final_answer": _json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, indent=2),
            "artifacts": [],
            "audit_summary": ["capability_router::human_confirmation_required"],
        }

    if (
        "stream probe" in low_goal
        or "canale5" in low_goal
        or "canale 5" in low_goal
        or "mediaset" in low_goal
        or "canale5" in low_skill
        or "canale 5" in low_skill
        or "mediaset" in low_skill
    ):
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
                            "planner": req.planner_model_profile,
                            "coder": req.coder_model_profile,
                            "judge": req.judge_model_profile,
                        },
                        "used_models": {
                            "planner": req.planner_model_name,
                            "coder": req.coder_model_name,
                            "judge": req.judge_model_name,
                        },
                        "used_rag": {
                            "planner": req.planner_rag_collection,
                            "coder": req.coder_rag_collection,
                            "judge": req.judge_rag_collection,
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
                    "planner": req.planner_model_profile,
                    "coder": req.coder_model_profile,
                    "judge": req.judge_model_profile,
                },
                "used_models": {
                    "planner": req.planner_model_name,
                    "coder": req.coder_model_name,
                    "judge": req.judge_model_name,
                },
                "used_rag": {
                    "planner": req.planner_rag_collection,
                    "coder": req.coder_rag_collection,
                    "judge": req.judge_rag_collection,
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
                    "planner": req.planner_model_profile,
                    "coder": req.coder_model_profile,
                    "judge": req.judge_model_profile,
                },
                "used_models": {
                    "planner": req.planner_model_name,
                    "coder": req.coder_model_name,
                    "judge": req.judge_model_name,
                },
                "used_rag": {
                    "planner": req.planner_rag_collection,
                    "coder": req.coder_rag_collection,
                    "judge": req.judge_rag_collection,
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
                "planner": req.planner_model_profile,
                "coder": req.coder_model_profile,
                "judge": req.judge_model_profile,
            },
            "used_models": {
                "planner": req.planner_model_name,
                "coder": req.coder_model_name,
                "judge": req.judge_model_name,
            },
            "used_rag": {
                "planner": req.planner_rag_collection,
                "coder": req.coder_rag_collection,
                "judge": req.judge_rag_collection,
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

    adapter = OpenShellAdapterReal(base_url=SELF_BASE_URL)

    planner = OllamaPlanner(
        base_url="http://127.0.0.1:11434",
        model=req.planner_model_name,
        fallback=DeterministicPlanner(),
        timeout_sec=60,
    )
    coder_planner = OllamaPlanner(
        base_url="http://127.0.0.1:11434",
        model=req.coder_model_name,
        fallback=DeterministicPlanner(),
        timeout_sec=60,
    )
    judge_planner = OllamaPlanner(
        base_url="http://127.0.0.1:11434",
        model=req.judge_model_name,
        fallback=DeterministicPlanner(),
        timeout_sec=60,
    )

    logger = AuditLogger(store_path=str(BASE_DIR / "agent-audit"))

    agent = RalfloopAgent(
        adapter=adapter,
        planner=planner,
        coder_planner=coder_planner,
        judge_planner=judge_planner,
        logger=logger,
    )

    extra_context = dict(req.extra_context or {})
    extra_context.setdefault("capability_route", capability_route)
    if any(
        marker in low_goal
        for marker in (
            "bando",
            "bandi",
            "candidatura",
            "arianna",
            "volontariato e territorio",
            "rld12025048623",
        )
    ):
        from ralfloop_agent.domains.bandi_runtime_context import (
            load_bandi_runtime_context,
        )

        extra_context["bandi_knowledge"] = load_bandi_runtime_context()

    state = agent.run(
        user_goal=req.user_goal,
        constraints=[],
        context={
            "skill_context": req.skill_context or "",
            "extra_context": extra_context,
            "capability_route": capability_route,
            "planner_model_profile": req.planner_model_profile,
            "coder_model_profile": req.coder_model_profile,
            "judge_model_profile": req.judge_model_profile,
            "planner_model_name": req.planner_model_name,
            "coder_model_name": req.coder_model_name,
            "judge_model_name": req.judge_model_name,
            "planner_rag_collection": req.planner_rag_collection,
            "coder_rag_collection": req.coder_rag_collection,
            "judge_rag_collection": req.judge_rag_collection,
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

    state.planner_model_profile = req.planner_model_profile
    state.coder_model_profile = req.coder_model_profile
    state.judge_model_profile = req.judge_model_profile
    state.planner_model_name = req.planner_model_name
    state.coder_model_name = req.coder_model_name
    state.judge_model_name = req.judge_model_name
    state.planner_rag_collection = req.planner_rag_collection
    state.coder_rag_collection = req.coder_rag_collection
    state.judge_rag_collection = req.judge_rag_collection

    envelope = ResultEnvelope(
        route=route_model,
        answer=state.final_answer,
        meta={"source": "ralfloop_agent", "stop_reason": state.stop_reason},
    )

    return {
        "ok": state.status == "completed",
        "mode": req.mode,
        "used_profiles": {
            "planner": req.planner_model_profile,
            "coder": req.coder_model_profile,
            "judge": req.judge_model_profile,
        },
        "used_models": {
            "planner": req.planner_model_name,
            "coder": req.coder_model_name,
            "judge": req.judge_model_name,
        },
        "used_rag": {
            "planner": req.planner_rag_collection,
            "coder": req.coder_rag_collection,
            "judge": req.judge_rag_collection,
        },
        "current_role": state.current_role,
        "role_history": state.role_history,
        "stop_reason": state.stop_reason,
        "capability_route": capability_route,
        "result_envelope": envelope.model_dump(mode="json"),
        "final_answer": state.final_answer,
        "artifacts": state.artifacts,
        "audit_summary": state.audit_summary,
    }


@app.post("/tasks/run")
def run_task(req: TaskRunRequest):
    from src.router import route_task as classify_task
    from src.skills import is_bandi_semantic_intent, is_local_maintenance_intent

    unified_source = (
        os.getenv("RALFLOOP_UNIFIED_ASSISTANT", "0") == "1"
        and str((req.extra_context or {}).get("source") or "").startswith("telegram_")
    )
    if unified_source:
        try:
            from ralfloop_agent.unified_assistant.runtime import is_unified_telegram_request

            if is_unified_telegram_request(req.user_goal, req.extra_context or {}):
                return _run_task_impl(req)
        except Exception:
            return _run_task_impl(req)
    classified = classify_task(req.user_goal)
    if req.mode == "route_only":
        return _run_task_impl(req)
    if classified.mode == "read_only_system_inspection":
        return _run_task_impl(req)
    if "abc_relation" in classified.mcp_used and classified.mode != "external_action":
        from ralfloop_agent.integration.capability_adapter import route_to_legacy_dict
        from ralfloop_agent.nodes.reasoning import run_capability_reasoning_cycle

        task_id = uuid.uuid4().hex
        envelope = run_capability_reasoning_cycle(
            req.user_goal,
            {"task_id": task_id, "mode": req.mode},
        )
        evidence = envelope.evidence
        ok = evidence is not None and evidence.exit_code == 0
        return {
            "ok": ok,
            "mode": req.mode,
            "current_role": "abc_relation_read",
            "role_history": ["capability_router", "abc_relation_read"],
            "stop_reason": "abc_relation_read_completed" if ok else "abc_relation_read_unavailable",
            "interaction_mode": "agent",
            "capability": "abc_relation",
            "external_action": False,
            "approval_required": False,
            "capability_route": route_to_legacy_dict(envelope.route),
            "result_envelope": envelope.model_dump(mode="json"),
            "final_answer": envelope.answer,
            "artifacts": [],
            "audit_summary": [
                f"abc_relation::read_only::{evidence.exit_code if evidence is not None else 'missing_evidence'}"
            ],
        }
    if (
        classified.mode == "external_action"
        and "browser" in classified.mcp_used
        and "bandi" in classified.skills_used
        and is_bandi_semantic_intent(req.user_goal)
        and not is_local_maintenance_intent(req.user_goal)
    ):
        return _run_task_impl(req)
    if classified.mode == "external_action" and is_local_maintenance_intent(req.user_goal):
        return _run_task_impl(req)
    if classified.mode == "external_action" and classified.requires_confirmation:
        return _run_task_impl(req)
    if classified.mode == "patch_allowed" and is_local_maintenance_intent(req.user_goal):
        # The coding harness owns its worker/judge lifecycle. Do not require the
        # generic shared-GPU handoff before a bounded local worktree patch.
        return _run_task_impl(req)

    from ralfloop_agent.providers.agent_gpu_handoff import AgentGpuCoordinator, AgentGpuHandoffError

    task_id = uuid.uuid4().hex
    models = (req.planner_model_name, req.coder_model_name, req.judge_model_name)
    coordinator = AgentGpuCoordinator(audit_fn=audit)
    try:
        with coordinator.agent_session(models=models, task_id=task_id):
            return _run_task_impl(req)
    except AgentGpuHandoffError as exc:
        audit("gpu_handoff_blocked", task_id=task_id, error=exc.code)
        raise HTTPException(status_code=503, detail=exc.code) from exc


@app.post("/confirmations/{confirmation_id}/approve")
def approve_confirmation(confirmation_id: str):
    from ralfloop_agent.integration.confirmation_store import confirm_action, get_confirmation
    from ralfloop_agent.integration.capability_adapter import mcp_client

    ok = confirm_action(confirmation_id)
    if not ok:
        return {"ok": False, "confirmation_id": confirmation_id, "executed": False}
    confirmation = get_confirmation(confirmation_id)
    return {
        "ok": True,
        "confirmation_id": confirmation_id,
        "executed": True,
        "confirmation": confirmation.__dict__ if confirmation else None,
        "message": mcp_client.execute_confirmed(confirmation_id),
    }


@app.post("/confirmations/{confirmation_id}/reject")
def reject_confirmation(confirmation_id: str):
    from ralfloop_agent.integration.confirmation_store import reject_action, get_confirmation

    ok = reject_action(confirmation_id)
    confirmation = get_confirmation(confirmation_id)
    return {
        "ok": ok,
        "confirmation_id": confirmation_id,
        "confirmation": confirmation.__dict__ if confirmation else None,
    }
