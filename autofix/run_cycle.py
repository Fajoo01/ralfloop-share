from __future__ import annotations
import json, sys
from autofix.core.coder import apply_fix_plan
from autofix.core.detector import should_autofix
from autofix.core.planner import choose_target, plan_fix
from autofix.core.promoter import promote_workspace_files, rebuild_registry, retry_payload
from autofix.core.validator import run_validator
from autofix.core.workspace import make_workspace

def main():
    if len(sys.argv) not in (4, 5):
        print("usage: run_cycle.py <user_goal> <stop_reason> <history_json> [validation_details_json]")
        raise SystemExit(2)

    user_goal = sys.argv[1]
    stop_reason = sys.argv[2]
    history = json.loads(sys.argv[3])
    validation_details = json.loads(sys.argv[4]) if len(sys.argv) >= 5 else {}
    if not isinstance(validation_details, dict):
        validation_details = {}

    print("VALIDATION_DETAILS=", json.dumps(validation_details, ensure_ascii=False))

    if not should_autofix(stop_reason, history):
        print("AUTOFIX_NOT_NEEDED")
        return

    suggested_target = str(validation_details.get("suggested_target") or "").strip()
    target = choose_target(user_goal, stop_reason, suggested_target)
    if not target:
        print("AUTOFIX_NO_TARGET")
        return

    ws = make_workspace(target)
    print("AUTOFIX_TARGET=", target["name"])
    print("AUTOFIX_WORKSPACE=", ws)

    max_attempts = 3
    last_after = None

    for attempt in range(1, max_attempts + 1):
        fix_plan = plan_fix(user_goal, stop_reason, history if isinstance(history, list) else [], validation_details, target)
        print("FIX_PLAN=", json.dumps(fix_plan, ensure_ascii=False))

        patch_res = apply_fix_plan(ws, target, fix_plan)
        print("PATCH_RES=", json.dumps(patch_res, ensure_ascii=False))

        if not patch_res.get("ok"):
            print("PATCH_FAILED")
            return

        current_after = json.dumps(patch_res.get("after_hashes") or {}, sort_keys=True, ensure_ascii=False)
        if last_after is not None and current_after == last_after:
            print("PATCH_NO_PROGRESS")
            print("AUTOFIX_MAX_ATTEMPTS_REACHED")
            return
        last_after = current_after

        ok, out = run_validator(target.get("validator_cmd", ""), workspace=ws)
        print("VALIDATOR_OK=", ok)
        print(out)

        if ok:
            promoted = promote_workspace_files(ws, target)
            print("PROMOTED_FILES=", json.dumps(promoted, ensure_ascii=False))
            rc, reg_out = rebuild_registry()
            print("REGISTRY_REBUILD_RC=", rc)
            print(reg_out)
            retry_payload_file = str(target.get("retry_payload_file", "")).strip()
            if retry_payload_file:
                rc2, retry_out = retry_payload(retry_payload_file)
                print("RETRY_RC=", rc2)
                print(retry_out)
            return

    print("AUTOFIX_MAX_ATTEMPTS_REACHED")

if __name__ == "__main__":
    main()
