from __future__ import annotations

import re
from typing import Any


_RAW_CODE_PATTERNS = (
    r"^```",
    r"\bwith\s+open\s*\(",
    r"\bprint\s*\(",
    r"^\s*(?:import|from)\s+\w+",
    r"^\s*(?:def|class)\s+\w+",
    r"cat\s*>",
    r"<<['\"]?\w+['\"]?",
    r"\bjson\.dump(?:s)?\s*\(",
    r"#!/(?:usr/bin/env\s+)?(?:python|bash|sh)",
)


def _goal_flags(goal: str) -> dict[str, bool]:
    goal = (goal or "").lower()
    return {
        "is_three_step": (
            ("scrivi" in goal or "write" in goal)
            and ("mostrami i file" in goal or "show me the files" in goal)
            and ("poi leggi" in goal or "then read" in goal)
        ),
        "is_two_step": (
            ("scrivi" in goal or "write" in goal)
            and ("poi leggi" in goal or "then read" in goal)
        ),
        "is_multi_file": ("leggili" in goal or "read them" in goal),
        "goal_targets_both_seeded": (
            "user_goal.txt" in goal and "skill_context.txt" in goal
        ),
    }


def _is_workspace_operational_goal(goal: str) -> bool:
    goal = (goal or "").lower()
    has_action = any(
        token in goal
        for token in (
            "scrivi",
            "write",
            "crea",
            "create",
            "trasforma",
            "transform",
            "aggiorna",
            "update",
            "modifica",
            "edit",
        )
    )
    has_workspace_target = (
        any(
            token in goal
            for token in ("file", "files", "cartella", "directory", "dir", "workspace", "json")
        )
        or bool(re.search(r"\b(?:out|tmp)/[^\s]+", goal))
        or bool(re.search(r"\b[\w.-]+\.(?:txt|md|json|log|csv|ya?ml)\b", goal))
    )
    return has_action and has_workspace_target


def _looks_like_raw_code(text: str) -> bool:
    body = (text or "").strip()
    if not body:
        return False
    return any(re.search(pattern, body, re.IGNORECASE | re.MULTILINE) for pattern in _RAW_CODE_PATTERNS)


def _read_paths_from_memory(state: Any) -> set[str]:
    return set(_latest_read_bodies_by_path(state))


def _latest_read_bodies_by_path(state: Any) -> dict[str, str]:
    latest: dict[str, str] = {}
    for mem in getattr(state, "memory", []) or []:
        content = getattr(mem, "content", "")
        if not isinstance(content, str) or not content.startswith("file_read::"):
            continue
        try:
            _, path, body = content.split("::", 2)
        except ValueError:
            continue
        latest[path.strip()] = body
    return latest


def _expected_reads(state: Any, planner: Any) -> int:
    write_pairs = []
    if hasattr(planner, "fallback") and hasattr(planner.fallback, "_extract_write_pairs"):
        write_pairs = planner.fallback._extract_write_pairs(state.user_goal)
    elif hasattr(planner, "_extract_write_pairs"):
        write_pairs = planner._extract_write_pairs(state.user_goal)
    return len(write_pairs) if write_pairs else 2


def evaluate_completion_stop(state: Any, planner: Any) -> bool:
    if getattr(state, "consecutive_failures", 0) >= 3:
        state.stop_reason = "repeated_failure"
        return True

    if getattr(state, "last_action", None) and getattr(state, "last_result", None) and state.last_result.ok:
        tool_name = state.last_action["tool_name"]
        flags = _goal_flags(getattr(state, "user_goal", ""))

        if tool_name in {"sandbox_read_file", "sandbox_http_fetch"}:
            if tool_name == "sandbox_read_file":
                latest_reads = _latest_read_bodies_by_path(state)
                read_paths = set(latest_reads)

                if flags["goal_targets_both_seeded"]:
                    if not {"user_goal.txt", "skill_context.txt"}.issubset(read_paths):
                        return False

                elif flags["is_multi_file"]:
                    expected_reads = _expected_reads(state, planner)
                    if len(read_paths) < expected_reads:
                        return False

                if _is_workspace_operational_goal(getattr(state, "user_goal", "")):
                    if flags["is_multi_file"]:
                        expected_reads = _expected_reads(state, planner)
                        meaningful = [body for body in latest_reads.values() if str(body or "").strip()]
                        if len(meaningful) < expected_reads:
                            return False
                        if any(_looks_like_raw_code(body) for body in meaningful):
                            return False
                    else:
                        if _looks_like_raw_code(getattr(state.last_result, "stdout", "")):
                            return False

            state.stop_reason = "goal_completed"
            return True

        if tool_name == "sandbox_write_file":
            if not flags["is_three_step"] and not flags["is_two_step"] and not flags["is_multi_file"]:
                state.stop_reason = "goal_completed"
                return True

        if tool_name == "sandbox_list_dir":
            if flags["is_three_step"]:
                return False
            if not flags["is_two_step"]:
                state.stop_reason = "goal_completed"
                return True

    history = getattr(state, "action_history", []) or []
    if len(history) >= 3:
        tail = history[-3:]
        first = tail[0]
        same_action = all(
            x.get("tool_name") == first.get("tool_name")
            and x.get("tool_input") == first.get("tool_input")
            for x in tail
        )
        same_stdout = bool(
            getattr(state, "last_result", None)
            and state.last_result.ok
            and (state.last_result.stdout or "").strip()
        )
        if same_action and same_stdout:
            state.stop_reason = "stalled_need_skill_patch"
            state.autofix_candidate = {
                "user_goal": state.user_goal,
                "stop_reason": state.stop_reason,
                "history": tail,
            }
            return True

    if getattr(state, "iteration", 0) + 1 >= getattr(state, "max_iterations", 0):
        state.stop_reason = "max_iterations_reached"
        return True

    return False
