from __future__ import annotations

import shlex
from typing import Any


def dispatch_tool(adapter: Any, sandbox: dict[str, Any], tool_name: str, tool_input: dict[str, Any]):
    tool_input = dict(tool_input or {})

    if tool_name == "sandbox_read_file":
        if "filename" in tool_input and "path" not in tool_input:
            tool_input["path"] = tool_input.pop("filename")

        path = str(tool_input.get("path", "") or "")
        if path.startswith("http://") or path.startswith("https://"):
            tool_name = "sandbox_http_fetch"
            tool_input = {"url": path, "method": "GET"}

    if tool_name == "sandbox_write_file":
        if "filename" in tool_input and "path" not in tool_input:
            tool_input["path"] = tool_input.pop("filename")

    if tool_name == "sandbox_exec":
        normalized_input = dict(tool_input or {})
        action = str(normalized_input.get("action") or "").strip().lower()
        file_path = str(normalized_input.get("file_path") or normalized_input.get("path") or "").strip()

        if "command" not in normalized_input or not normalized_input.get("command"):
            if action in {"inspect", "inspect_file", "ispect_file", "read_file", "show_file"} and file_path:
                normalized_input["command"] = f"cat {shlex.quote(file_path)}"

        return adapter.exec(sandbox, **normalized_input)
    if tool_name == "sandbox_write_file":
        return adapter.write_file(sandbox, **tool_input)
    if tool_name == "sandbox_read_file":
        return adapter.read_file(sandbox, **tool_input)
    if tool_name == "sandbox_list_dir":
        return adapter.list_dir(sandbox, **tool_input)
    if tool_name == "sandbox_http_fetch":
        return adapter.http_fetch(sandbox, **tool_input)
    raise ValueError(f"Unsupported tool: {tool_name}")
