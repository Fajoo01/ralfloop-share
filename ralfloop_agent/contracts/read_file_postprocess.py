from __future__ import annotations

from typing import Any

from ralfloop_agent.core.state import MemoryEntry
from ralfloop_agent.adapters.openshell_adapter import ToolResult


def handle_read_file_result(state: Any, decision: Any, result: Any) -> dict[str, Any]:
    raw_path = decision.tool_input.get("path")
    if raw_path is None:
        raw_path = decision.tool_input.get("filename")
    path = str(raw_path or "")

    if path.startswith("http://") or path.startswith("https://"):
        decision.tool_name = "sandbox_http_fetch"
        decision.tool_input = {"url": path, "method": "GET"}
        return {
            "result": result,
            "should_continue": False,
            "force_role_advance": False,
            "force_iteration_advance": False,
            "stop_now": False,
        }

    if not path or path.startswith("/") or ".." in path:
        denied = ToolResult(
            tool_name=decision.tool_name,
            ok=False,
            stdout="",
            stderr="policy_denied",
        )
        state.consecutive_failures += 1
        state.last_action = decision.model_dump()
        state.last_result = denied
        state.audit_summary.append(f"{decision.tool_name}: policy_denied")
        return {
            "result": denied,
            "should_continue": True,
            "force_role_advance": True,
            "force_iteration_advance": True,
            "stop_now": False,
        }

    if result.ok:
        state.memory.append(
            MemoryEntry(
                kind="result",
                content=f"file_read::{path}::{result.stdout}",
            )
        )

    return {
        "result": result,
        "should_continue": True,
        "force_role_advance": True,
        "force_iteration_advance": True,
        "stop_now": False,
    }
