from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ralfloop_agent.core.decisions import ActionDecision


_PATH_PATTERN = re.compile(r"\b(?:out|tmp|tools|tests|openshell_backend|ralfloop_agent)/[^\s,;:()]+|\b[\w./-]+\.(?:txt|md|json|log|csv|ya?ml|py)\b")


def _looks_like_path(value: str) -> bool:
    value = str(value or "").strip()
    if not value:
        return False
    return bool(_PATH_PATTERN.search(value))


def _planner_extract_write_pairs(planner: Any, goal: str) -> list[tuple[str, str]]:
    if planner is None:
        return []
    if hasattr(planner, "_extract_write_pairs"):
        return planner._extract_write_pairs(goal)
    fallback = getattr(planner, "fallback", None)
    if fallback is not None and hasattr(fallback, "_extract_write_pairs"):
        return fallback._extract_write_pairs(goal)
    return []


def _normalized_explicit_specs(goal: str, planner: Any) -> dict[str, str]:
    specs: dict[str, str] = {}

    for first, second in _planner_extract_write_pairs(planner, goal):
        if _looks_like_path(second):
            specs[str(second).strip()] = str(first)
            continue
        if _looks_like_path(first):
            specs[str(first).strip()] = str(second)

    patterns = [
        r'((?:out|tmp|tools|tests|openshell_backend|ralfloop_agent)/[^\s,;:()]+|[\w./-]+\.(?:txt|md|json|log|csv|ya?ml|py))\s+con\s+"([^"]+)"',
        r"((?:out|tmp|tools|tests|openshell_backend|ralfloop_agent)/[^\s,;:()]+|[\w./-]+\.(?:txt|md|json|log|csv|ya?ml|py))\s+con\s+'([^']+)'",
        r'"([^"]+)"\s+in\s+((?:out|tmp|tools|tests|openshell_backend|ralfloop_agent)/[^\s,;:()]+|[\w./-]+\.(?:txt|md|json|log|csv|ya?ml|py))',
        r"'([^']+)'\s+in\s+((?:out|tmp|tools|tests|openshell_backend|ralfloop_agent)/[^\s,;:()]+|[\w./-]+\.(?:txt|md|json|log|csv|ya?ml|py))",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, goal, flags=re.IGNORECASE):
            first = match.group(1).strip().rstrip('.,;:')
            second = match.group(2).strip().rstrip('.,;:')
            if _looks_like_path(first) and not _looks_like_path(second):
                specs[first] = second
            elif _looks_like_path(second):
                specs[second] = first

    single_file_body = re.search(
        r'((?:out|tmp|tools|tests|openshell_backend|ralfloop_agent)/[^\s,;:()]+|[\w./-]+\.(?:txt|md|json|log|csv|ya?ml|py))\s+che\s+contenga\s+solo\s+(.+?)(?:\s+e\s+poi\b|\s+poi\b|$)',
        goal,
        flags=re.IGNORECASE,
    )
    if single_file_body:
        path = single_file_body.group(1).strip().rstrip('.,;:')
        body = single_file_body.group(2).strip()
        if body and _looks_like_path(path):
            specs[path] = body

    return specs


def requested_file_specs(goal: str, planner: Any) -> dict[str, str]:
    specs = _normalized_explicit_specs(goal, planner)

    requested_paths: list[str] = []
    for match in _PATH_PATTERN.finditer(goal):
        path = match.group(0).rstrip('.,;:')
        if path not in requested_paths:
            requested_paths.append(path)

    for path in requested_paths:
        if path not in specs:
            stem = Path(path).stem.replace("_", " ").replace("-", " ").strip() or "file"
            specs[path] = f"contenuto {stem}"

    return specs


def read_paths_from_memory(state: Any) -> set[str]:
    read_paths: set[str] = set()
    for mem in getattr(state, "memory", []) or []:
        content = getattr(mem, "content", "")
        if not isinstance(content, str) or not content.startswith("file_read::"):
            continue
        try:
            _, path, _body = content.split("::", 2)
        except ValueError:
            continue
        read_paths.add(path.strip())
    return read_paths


def _requested_existing_paths(state: Any, requested_paths: list[str]) -> set[str]:
    root = Path(getattr(getattr(state, "sandbox", None), "workspace_path", "") or "")
    if not root:
        return set()
    existing: set[str] = set()
    for path in requested_paths:
        if (root / path.lstrip("/")).exists():
            existing.add(path)
    return existing


def _decision_writes_path(decision: ActionDecision, path: str) -> bool:
    if decision.tool_name == "sandbox_write_file":
        tool_input = dict(decision.tool_input or {})
        return str(tool_input.get("path", "")).strip() == path
    if decision.tool_name != "sandbox_exec":
        return False
    tool_input = dict(decision.tool_input or {})
    command = str(tool_input.get("command", ""))
    return bool(command and path in command)


def _append_runtime_debug(state: Any, entry: dict[str, Any]) -> None:
    ctx = getattr(state, "context", None)
    if not isinstance(ctx, dict):
        return
    runtime_debug = ctx.setdefault("runtime_debug", [])
    if not isinstance(runtime_debug, list):
        runtime_debug = []
        ctx["runtime_debug"] = runtime_debug
    runtime_debug.append(entry)
    if len(runtime_debug) > 20:
        del runtime_debug[:-20]


def apply_runtime_multifile_guard(state: Any, decision: ActionDecision, planner: Any) -> ActionDecision:
    goal_low = (getattr(state, "user_goal", "") or "").lower()
    is_operational = any(token in goal_low for token in ("scrivi", "write", "crea", "create", "modifica", "edit", "aggiorna", "update"))
    wants_multi_read = ("leggili" in goal_low or "read them" in goal_low)
    wants_single_read = ("leggilo" in goal_low or "read it" in goal_low)

    specs = requested_file_specs(getattr(state, "user_goal", "") or "", planner)
    requested_paths = list(specs)
    read_paths = sorted(read_paths_from_memory(state))
    existing_paths = sorted(_requested_existing_paths(state, requested_paths))

    debug = {
        "iteration": getattr(state, "iteration", 0),
        "role": getattr(state, "current_role", ""),
        "requested_paths": requested_paths,
        "read_paths": read_paths,
        "existing_paths": existing_paths,
        "incoming_tool": decision.tool_name,
        "incoming_input": dict(decision.tool_input or {}),
        "action": "keep",
    }

    wants_followup_read = wants_multi_read or wants_single_read

    if not is_operational or not wants_followup_read or not requested_paths:
        _append_runtime_debug(state, debug)
        return decision

    missing_paths = [path for path in requested_paths if path not in existing_paths]
    if missing_paths:
        next_path = missing_paths[0]
        if not _decision_writes_path(decision, next_path):
            content = specs[next_path]
            if not content.endswith("\n"):
                content += "\n"
            debug["action"] = "write_missing"
            debug["chosen_path"] = next_path
            _append_runtime_debug(state, debug)
            return ActionDecision(
                tool_name="sandbox_write_file",
                tool_input={"path": next_path, "content": content},
                why=f"Creo il prossimo file richiesto mancante: {next_path}.",
            )
        debug["action"] = "keep_write_missing"
        debug["chosen_path"] = next_path
        _append_runtime_debug(state, debug)
        return decision

    unread_paths = [path for path in requested_paths if path not in read_paths]
    if unread_paths:
        next_path = unread_paths[0]
        current_path = str((decision.tool_input or {}).get("path", "")).strip()
        if decision.tool_name != "sandbox_read_file" or current_path != next_path:
            debug["action"] = "read_unread"
            debug["chosen_path"] = next_path
            _append_runtime_debug(state, debug)
            return ActionDecision(
                tool_name="sandbox_read_file",
                tool_input={"path": next_path},
                why=f"Leggo il prossimo file richiesto non ancora letto: {next_path}.",
            )
        debug["action"] = "keep_read_unread"
        debug["chosen_path"] = next_path
        _append_runtime_debug(state, debug)
        return decision

    debug["action"] = "all_requested_files_read"
    _append_runtime_debug(state, debug)
    return decision
