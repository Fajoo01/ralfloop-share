from __future__ import annotations

def choose_target(user_goal: str, stop_reason: str, suggested_target: str = "") -> dict | None:
    sug = str(suggested_target or "").strip().lower()
    goal = str(user_goal or "").lower()
    reason = str(stop_reason or "").strip().lower()

    if sug == "grammar" or "analisi grammaticale" in goal:
        return {
            "name": "grammar",
            "patch_files": ["openshell_backend/skill_grammar_rag.py"],
            "validator_cmd": "PYTHONPATH=. python3 tests/grammar/run_grammar_tests.py",
        }

    if reason in {"skill_output_insufficient", "stalled_need_skill_patch"} and "grammaticale" in goal:
        return {
            "name": "grammar",
            "patch_files": ["openshell_backend/skill_grammar_rag.py"],
            "validator_cmd": "PYTHONPATH=. python3 tests/grammar/run_grammar_tests.py",
        }

    return None


def plan_fix(
    user_goal: str,
    stop_reason: str,
    history: list[dict],
    validation_details: dict,
    target: dict,
) -> dict:
    target_file = str(validation_details.get("suggested_file") or (target.get("patch_files") or [""])[0])
    required_changes = validation_details.get("required_changes") or []
    if not isinstance(required_changes, list):
        required_changes = []

    details = validation_details.get("details") or []
    if not isinstance(details, list):
        details = []

    why = str(validation_details.get("reason") or stop_reason or "unknown")

    return {
        "target_name": str(target.get("name") or ""),
        "target_file": target_file,
        "strategy": "deterministic_patch" if str(target.get("name") or "") == "grammar" else "generic_patch",
        "why": why,
        "required_changes": required_changes,
        "constraints": [
            "preserve current json output structure",
            "modify only workspace files",
            "must pass validator_cmd",
        ],
        "test_command": str(target.get("validator_cmd") or ""),
        "validation_details": details,
        "history": history or [],
        "user_goal": user_goal,
    }
