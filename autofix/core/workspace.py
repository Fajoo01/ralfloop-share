from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

BASE = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")
WORKSPACES = BASE / "autofix" / "workspaces"

def _copytree_filtered(src: Path, dst: Path, ignore_names: set[str] | None = None):
    ignore_names = ignore_names or set()
    def _ignore(path, names):
        return {n for n in names if n in ignore_names}
    shutil.copytree(src, dst, ignore=_ignore)

def make_workspace(target: dict) -> str:
    name = str(target.get("name") or "generic").strip() or "generic"
    ws = WORKSPACES / f"{name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if ws.exists():
        shutil.rmtree(ws)
    ws.mkdir(parents=True, exist_ok=True)

    _copytree_filtered(BASE / "openshell_backend", ws / "openshell_backend", {"__pycache__"})
    _copytree_filtered(BASE / "tests", ws / "tests", {"__pycache__"})
    _copytree_filtered(BASE / "autofix", ws / "autofix", {"workspaces", "__pycache__"})

    return str(ws)
