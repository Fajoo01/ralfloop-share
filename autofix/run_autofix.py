from __future__ import annotations
import json
import sys
from pathlib import Path

from autofix.core.detector import should_autofix
from autofix.core.planner import choose_target
from autofix.core.workspace import make_workspace
from autofix.core.validator import run_validator

def main():
    if len(sys.argv) != 4:
        print("usage: run_autofix.py <user_goal> <stop_reason> <history_json>")
        raise SystemExit(2)

    user_goal = sys.argv[1]
    stop_reason = sys.argv[2]
    history = json.loads(sys.argv[3])

    if not should_autofix(stop_reason, history):
        print("AUTOFIX_NOT_NEEDED")
        return

    target = choose_target(user_goal, stop_reason)
    if not target:
        print("AUTOFIX_NO_TARGET")
        return

    ws = make_workspace(target)
    print("AUTOFIX_TARGET=", target["name"])
    print("AUTOFIX_WORKSPACE=", ws)

    ok, out = run_validator(target.get("validator_cmd", ""))
    print("VALIDATOR_OK=", ok)
    print(out)

if __name__ == "__main__":
    main()
