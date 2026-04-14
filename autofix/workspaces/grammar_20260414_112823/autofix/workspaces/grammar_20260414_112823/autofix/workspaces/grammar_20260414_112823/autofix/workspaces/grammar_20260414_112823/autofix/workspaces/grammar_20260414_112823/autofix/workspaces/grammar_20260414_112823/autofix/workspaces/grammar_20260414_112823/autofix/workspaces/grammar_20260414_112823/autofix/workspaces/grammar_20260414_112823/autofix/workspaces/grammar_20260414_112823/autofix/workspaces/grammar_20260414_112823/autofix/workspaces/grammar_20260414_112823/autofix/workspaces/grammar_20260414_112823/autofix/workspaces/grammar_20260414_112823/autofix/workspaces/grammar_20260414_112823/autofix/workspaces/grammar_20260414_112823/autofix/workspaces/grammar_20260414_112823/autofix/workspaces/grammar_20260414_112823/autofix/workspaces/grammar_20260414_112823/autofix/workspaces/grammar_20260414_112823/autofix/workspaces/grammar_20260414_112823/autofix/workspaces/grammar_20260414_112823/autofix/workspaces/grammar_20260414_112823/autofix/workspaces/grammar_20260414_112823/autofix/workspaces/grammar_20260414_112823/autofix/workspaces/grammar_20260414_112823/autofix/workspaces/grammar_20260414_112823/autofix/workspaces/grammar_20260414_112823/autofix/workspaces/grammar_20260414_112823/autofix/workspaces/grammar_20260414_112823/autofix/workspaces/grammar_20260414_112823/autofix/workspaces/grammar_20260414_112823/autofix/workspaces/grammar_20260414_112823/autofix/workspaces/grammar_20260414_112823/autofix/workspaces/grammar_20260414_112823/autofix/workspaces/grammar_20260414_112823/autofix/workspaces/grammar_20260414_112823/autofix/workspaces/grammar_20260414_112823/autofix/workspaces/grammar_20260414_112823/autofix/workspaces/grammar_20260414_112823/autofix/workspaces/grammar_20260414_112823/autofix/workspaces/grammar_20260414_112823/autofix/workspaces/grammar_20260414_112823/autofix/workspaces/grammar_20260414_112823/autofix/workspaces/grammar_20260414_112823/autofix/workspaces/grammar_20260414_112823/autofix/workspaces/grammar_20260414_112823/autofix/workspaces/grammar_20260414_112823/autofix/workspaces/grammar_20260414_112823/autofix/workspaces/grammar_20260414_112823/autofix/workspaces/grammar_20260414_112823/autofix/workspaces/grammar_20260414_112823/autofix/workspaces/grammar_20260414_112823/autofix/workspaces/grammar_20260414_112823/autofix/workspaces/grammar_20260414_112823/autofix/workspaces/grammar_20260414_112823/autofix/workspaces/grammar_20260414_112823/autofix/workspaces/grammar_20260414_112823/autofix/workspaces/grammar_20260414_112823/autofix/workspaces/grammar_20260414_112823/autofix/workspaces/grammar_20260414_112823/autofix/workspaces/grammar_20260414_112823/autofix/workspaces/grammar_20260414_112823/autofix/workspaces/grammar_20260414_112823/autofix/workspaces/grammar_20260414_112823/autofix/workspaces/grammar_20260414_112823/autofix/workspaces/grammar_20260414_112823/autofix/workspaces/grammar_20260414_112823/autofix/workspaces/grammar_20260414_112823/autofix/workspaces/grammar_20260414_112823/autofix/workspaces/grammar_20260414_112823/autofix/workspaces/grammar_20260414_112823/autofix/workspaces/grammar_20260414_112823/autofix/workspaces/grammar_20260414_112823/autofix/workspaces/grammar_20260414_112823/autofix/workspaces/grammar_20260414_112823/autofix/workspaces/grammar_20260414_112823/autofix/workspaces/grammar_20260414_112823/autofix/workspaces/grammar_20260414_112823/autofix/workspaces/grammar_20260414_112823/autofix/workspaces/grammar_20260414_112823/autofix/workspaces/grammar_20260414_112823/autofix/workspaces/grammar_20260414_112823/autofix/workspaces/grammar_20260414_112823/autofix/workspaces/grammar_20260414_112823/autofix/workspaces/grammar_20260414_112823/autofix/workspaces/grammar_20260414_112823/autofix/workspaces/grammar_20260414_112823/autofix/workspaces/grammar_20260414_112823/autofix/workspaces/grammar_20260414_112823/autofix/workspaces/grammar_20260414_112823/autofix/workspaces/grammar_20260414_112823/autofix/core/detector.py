from __future__ import annotations

def should_autofix(stop_reason: str, history: list[dict]) -> bool:
    reason = str(stop_reason or "").strip()
    if reason in {
        "stalled_need_skill_patch",
        "skill_output_insufficient",
        "repeated_failure",
        "max_iterations_reached",
    }:
        return True

    for h in history or []:
        if not isinstance(h, dict):
            continue
        tool_name = str(h.get("tool_name") or "").strip()
        if tool_name.startswith("skill::"):
            return True

    return False
