from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

BASE_DIR = Path("/home/sibilla-cumana/ralfloop_agent_scaffold/.openshell_backend")
BASE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Ralfloop OpenShell Backend")


class SandboxCreateResponse(BaseModel):
    id: str
    root: str
    status: str


@app.post("/sandboxes", response_model=SandboxCreateResponse)
def create_sandbox():
    sid = str(uuid.uuid4())
    root = BASE_DIR / sid / "workspace"
    (root / "out").mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(parents=True, exist_ok=True)
    return SandboxCreateResponse(id=sid, root=str(root), status="ready")


@app.delete("/sandboxes/{sid}")
def destroy_sandbox(sid: str):
    target = BASE_DIR / sid
    if target.exists():
        shutil.rmtree(target)
    return {"ok": True}


@app.get("/sandboxes/{sid}/list")
def list_dir(sid: str, path: str = Query(".")):
    root = BASE_DIR / sid / "workspace"
    if not root.exists():
        raise HTTPException(status_code=404, detail="sandbox_not_found")

    rel = path.lstrip("/")
    full = (root / rel).resolve()

    if root.resolve() not in full.parents and full != root.resolve():
        raise HTTPException(status_code=403, detail="path_not_allowed")

    if not full.exists():
        raise HTTPException(status_code=404, detail="path_not_found")

    if not full.is_dir():
        raise HTTPException(status_code=400, detail="not_a_directory")

    entries = []
    for p in sorted(full.iterdir()):
        entries.append(f"{'d' if p.is_dir() else 'f'} {p.name}")

    return {
        "ok": True,
        "path": path,
        "entries": entries,
    }
