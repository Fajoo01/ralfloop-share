from __future__ import annotations

import json
import re
from typing import Any


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

        collected: list[tuple[str, str]] = []
        for mem in state.memory:
            content = getattr(mem, "content", "")
            if content.startswith("file_read::"):
                try:
                    _, path, body = content.split("::", 2)
                except ValueError:
                    continue
                collected.append((path, body))

        if wants_seeded_context and collected:
            by_path = {path: body for path, body in collected}
            parts = []
            if "user_goal.txt" in by_path:
                parts.append("user_goal.txt:\n" + by_path["user_goal.txt"].strip())
            if "skill_context.txt" in by_path:
                parts.append("skill_context.txt:\n" + by_path["skill_context.txt"].strip())
            if parts:
                return "Contenuto dei file richiesti:\n\n" + "\n\n".join(parts)

        if is_multi_file and collected:
            return "Contenuto dei file:\n" + "\n\n".join(
                f"{path}:\n{body}" for path, body in collected
            )

        return "Contenuto del file:\n" + result.stdout.strip()

    return f"Task completato. Ultimo tool: {result.tool_name}. Output:\n{result.stdout.strip()}"
