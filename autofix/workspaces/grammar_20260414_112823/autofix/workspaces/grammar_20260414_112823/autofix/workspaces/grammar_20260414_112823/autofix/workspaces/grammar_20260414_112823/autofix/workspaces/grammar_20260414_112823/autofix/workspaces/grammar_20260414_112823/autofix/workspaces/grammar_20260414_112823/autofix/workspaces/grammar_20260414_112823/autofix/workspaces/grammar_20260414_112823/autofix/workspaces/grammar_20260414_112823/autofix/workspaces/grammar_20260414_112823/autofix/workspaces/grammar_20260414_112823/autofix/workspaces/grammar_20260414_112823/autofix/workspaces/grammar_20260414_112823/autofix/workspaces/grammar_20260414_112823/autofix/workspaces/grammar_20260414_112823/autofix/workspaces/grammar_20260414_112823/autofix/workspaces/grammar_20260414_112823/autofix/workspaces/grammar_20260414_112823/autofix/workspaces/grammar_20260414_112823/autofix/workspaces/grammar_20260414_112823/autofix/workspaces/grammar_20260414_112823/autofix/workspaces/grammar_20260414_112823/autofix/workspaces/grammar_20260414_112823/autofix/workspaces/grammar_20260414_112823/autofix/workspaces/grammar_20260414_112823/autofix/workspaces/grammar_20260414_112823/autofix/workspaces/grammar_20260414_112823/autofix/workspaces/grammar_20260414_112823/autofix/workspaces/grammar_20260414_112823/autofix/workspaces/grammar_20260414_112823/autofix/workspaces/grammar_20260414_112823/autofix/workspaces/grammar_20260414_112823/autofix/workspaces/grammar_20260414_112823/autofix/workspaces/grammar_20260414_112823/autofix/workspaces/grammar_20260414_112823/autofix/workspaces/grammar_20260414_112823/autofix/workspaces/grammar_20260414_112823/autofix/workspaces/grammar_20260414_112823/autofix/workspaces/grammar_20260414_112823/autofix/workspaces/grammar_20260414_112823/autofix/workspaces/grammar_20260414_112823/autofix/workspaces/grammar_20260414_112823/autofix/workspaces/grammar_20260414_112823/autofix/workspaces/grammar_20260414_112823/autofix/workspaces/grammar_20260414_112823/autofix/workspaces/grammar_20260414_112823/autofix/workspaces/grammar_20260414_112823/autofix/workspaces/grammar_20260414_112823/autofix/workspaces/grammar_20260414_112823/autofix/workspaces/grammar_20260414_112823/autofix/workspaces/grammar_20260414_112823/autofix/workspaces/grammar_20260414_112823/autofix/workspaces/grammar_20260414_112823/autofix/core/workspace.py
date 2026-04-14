from __future__ import annotations
import shutil
from pathlib import Path

BASE = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
WORKSPACES = BASE / "autofix" / "workspaces"

def make_workspace(target: dict) -> str:
    from datetime import datetime
    name = str(target.get("name") or "generic").strip() or "generic"
    ws = WORKSPACES / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)

    for item in ["openshell_backend", "tests", "autofix"]:
        src = BASE / item
        dst = ws / item
        if src.is_dir():
            shutil.copytree(src, dst)
    return str(ws)
