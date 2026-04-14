from __future__ import annotations
import json
import subprocess
import sys

def main():
    if len(sys.argv) != 2:
        print("usage: run_from_tasks_run.py <tasks_run_json_file>")
        raise SystemExit(2)

    path = sys.argv[1]
    data = json.load(open(path, "r", encoding="utf-8"))

    cand = data.get("autofix_candidate") or {}
    user_goal = str(cand.get("user_goal") or data.get("user_goal") or "")
    stop_reason = str(data.get("stop_reason") or "")
    history = cand.get("history") if isinstance(cand, dict) else []
    if not isinstance(history, list):
        history = []

    validation_details = cand.get("validation_details") if isinstance(cand, dict) else {}
    if not isinstance(validation_details, dict):
        validation_details = {}

    cmd = [
        sys.executable,
        "-m",
        "autofix.run_cycle",
        user_goal,
        stop_reason,
        json.dumps(history, ensure_ascii=False),
        json.dumps(validation_details, ensure_ascii=False),
    ]

    r = subprocess.run(cmd, text=True, capture_output=True)
    print(r.stdout, end="")
    if r.stderr:
        print(r.stderr, end="", file=sys.stderr)
    raise SystemExit(r.returncode)

if __name__ == "__main__":
    main()
