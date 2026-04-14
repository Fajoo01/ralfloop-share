from __future__ import annotations
import os
import subprocess

def run_validator(cmd: str, workspace: str = "") -> tuple[bool, str]:
    if not cmd.strip():
        return False, "missing validator"

    env = os.environ.copy()
    if workspace:
        env["AUTOFIX_WORKSPACE"] = workspace
        repo = os.getcwd()

    r = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        env=env,
    )
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    return r.returncode == 0, out.strip()
