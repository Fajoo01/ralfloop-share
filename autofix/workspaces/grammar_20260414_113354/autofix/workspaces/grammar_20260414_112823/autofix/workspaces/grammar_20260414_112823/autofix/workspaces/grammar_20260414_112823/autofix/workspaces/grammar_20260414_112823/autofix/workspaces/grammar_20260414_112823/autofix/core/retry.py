from __future__ import annotations
import subprocess

def retry_payload_file(path: str) -> tuple[int, str]:
    cmd = f"curl -sS -X POST http://127.0.0.1:19090/tasks/run -H 'Content-Type: application/json' --data-binary @{path}"
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    return r.returncode, (r.stdout or "") + (("\n" + r.stderr) if r.stderr else "")
