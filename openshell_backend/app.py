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
