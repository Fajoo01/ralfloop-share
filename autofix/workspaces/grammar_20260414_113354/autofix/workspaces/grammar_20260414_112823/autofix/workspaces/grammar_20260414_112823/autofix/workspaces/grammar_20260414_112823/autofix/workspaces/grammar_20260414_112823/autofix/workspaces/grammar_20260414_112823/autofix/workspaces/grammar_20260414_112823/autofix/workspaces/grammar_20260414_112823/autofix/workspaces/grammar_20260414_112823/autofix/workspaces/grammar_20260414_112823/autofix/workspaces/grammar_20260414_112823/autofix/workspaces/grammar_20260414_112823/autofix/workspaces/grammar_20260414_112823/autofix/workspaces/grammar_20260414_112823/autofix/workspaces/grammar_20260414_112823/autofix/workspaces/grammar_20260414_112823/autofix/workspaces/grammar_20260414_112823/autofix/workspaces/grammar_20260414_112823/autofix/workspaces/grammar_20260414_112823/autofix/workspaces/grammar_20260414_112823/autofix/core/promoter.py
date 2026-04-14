from __future__ import annotations

import subprocess
import sys
from pathlib import Path

BASE = Path("/home/sibilla-cumana/ralfloop_agent_scaffold")

def promote_workspace_file(workspace_file: Path, target_relpath: str):
    dst = BASE / target_relpath
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        bak = dst.with_name(dst.name + ".before_autofix_promote")
        bak.write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
    dst.write_text(workspace_file.read_text(encoding="utf-8"), encoding="utf-8")
    return str(dst)

def promote_workspace_files(ws: str, target: dict) -> list[str]:
    ws_path = Path(ws)
    promoted: list[str] = []
    for rel in target.get("patch_files", []) or []:
        src = ws_path / Path(rel).name
        if not src.exists():
            continue
        promoted.append(promote_workspace_file(src, rel))
    return promoted

def rebuild_registry() -> tuple[int, str]:
    cmd = [sys.executable, str(BASE / "autofix" / "build_skill_registry.py")]
    r = subprocess.run(cmd, text=True, capture_output=True, cwd=str(BASE))
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    return r.returncode, out.strip()

def retry_payload(payload_path: str) -> tuple[int, str]:
    cmd = (
        "curl -sS -X POST http://127.0.0.1:19090/tasks/run "
        "-H 'Content-Type: application/json' "
        f"--data-binary @{payload_path}"
    )
    r = subprocess.run(cmd, shell=True, text=True, capture_output=True, cwd=str(BASE))
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    return r.returncode, out.strip()
