from __future__ import annotations

import os
import subprocess

def run_validator(cmd: str, workspace: str = "") -> tuple[bool, str]:
    env = dict(os.environ)
    cwd = os.getcwd()

    if workspace:
        env["AUTOFIX_WORKSPACE"] = workspace
        env["PYTHONPATH"] = workspace
        cwd = workspace

    r = subprocess.run(cmd, shell=True, text=True, capture_output=True, env=env, cwd=cwd)
    out = (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
    return r.returncode == 0, out.strip()
