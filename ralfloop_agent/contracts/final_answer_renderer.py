from __future__ import annotations

import json
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


def _latest_read_bodies_by_path(state: Any) -> dict[str, str]:
    latest: dict[str, str] = {}
    for mem in state.memory:
        content = getattr(mem, "content", "")
        if not content.startswith("file_read::"):
            continue
        try:
            _, path, body = content.split("::", 2)
        except ValueError:
            continue
        latest[path] = body
    return latest


def render_final_answer(state: Any) -> str:
    result = state.last_result
    if result is None:
        return "Nessun risultato disponibile."

    if result.tool_name == "sandbox_http_fetch":
        try:
            payload = json.loads(result.stdout)
            text_parts = []

            if isinstance(payload, dict):
                for k in ("body", "text", "stdout", "content"):
                    v = payload.get(k)
                    if isinstance(v, str):
                        text_parts.append(v)

            blob = "\n".join(text_parts)
            m = re.search(r'https?://[^\s"\']+\.(?:m3u8|mpd)[^\s"\']*', blob, re.I)
            if m:
                return json.dumps({"stream_url": m.group(0), "headers": {}}, ensure_ascii=False)

            if isinstance(payload, dict) and isinstance(payload.get("models"), list):
                names = [
                    m.get("name")
                    for m in payload["models"]
                    if isinstance(m, dict) and m.get("name")
                ]
                if names:
                    return "Modelli Ollama disponibili:\n" + "\n".join(f"- {name}" for name in names)
        except Exception:
            pass

    if result.tool_name == "sandbox_list_dir":
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        requested_path = "."
        if state.last_action and isinstance(state.last_action, dict):
            requested_path = state.last_action.get("tool_input", {}).get("path", ".")
        if lines:
            title = (
                "Contenuto della workspace:"
                if requested_path == "."
                else f"Contenuto della directory {requested_path}:"
            )
            return title + "\n" + "\n".join(f"- {line}" for line in lines)
        return "La workspace è vuota." if requested_path == "." else f"La directory {requested_path} è vuota."

    if result.tool_name == "sandbox_read_file":
        goal = state.user_goal.lower()
        is_multi_file = ("leggili" in goal or "read them" in goal)
        wants_seeded_context = ("user_goal.txt" in goal and "skill_context.txt" in goal)
        is_operational = _is_workspace_operational_goal(state.user_goal)
        latest_reads = _latest_read_bodies_by_path(state)

        if wants_seeded_context and latest_reads:
            parts = []
            if "user_goal.txt" in latest_reads:
                parts.append("user_goal.txt:\n" + latest_reads["user_goal.txt"].strip())
            if "skill_context.txt" in latest_reads:
                parts.append("skill_context.txt:\n" + latest_reads["skill_context.txt"].strip())
            if parts:
                return "Contenuto dei file richiesti:\n\n" + "\n\n".join(parts)

        if is_multi_file and latest_reads:
            visible = [
                (path, body)
                for path, body in latest_reads.items()
                if not is_operational or not _looks_like_raw_code(body)
            ]
            if visible:
                return "Contenuto dei file:\n" + "\n\n".join(
                    f"{path}:\n{body}" for path, body in visible
                )
            if is_operational:
                return "Task operativo non verificato: i contenuti letti sembrano solo codice grezzo non eseguito."

        body = result.stdout.strip()
        if is_operational and _looks_like_raw_code(body):
            return "Task operativo non verificato: il contenuto letto sembra solo codice grezzo non eseguito."
        return "Contenuto del file:\n" + body

    output = result.stdout.strip()
    if _is_workspace_operational_goal(getattr(state, "user_goal", "")) and _looks_like_raw_code(output):
        return "Task operativo non verificato: l'ultimo output sembra codice grezzo, non un risultato del workspace."
    return f"Task completato. Ultimo tool: {result.tool_name}. Output:\n{output}"
