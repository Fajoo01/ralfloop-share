from __future__ import annotations

from typing import Any, Mapping

from .runtsuite_adapter import RuntsuiteReadOnlyAdapter
from .service_identity_mcp import _error, _result, _tool, serve


TOOLS = {
    "runtsuite_get_member": "get_member",
    "runtsuite_list_projects": "list_projects",
    "runtsuite_list_funding_calls": "list_funding_calls",
    "runtsuite_list_meetings": "list_meetings",
    "runtsuite_list_attendance": "list_attendance",
    "runtsuite_list_member_cards": "list_member_cards",
    "runtsuite_list_member_account_links": "list_member_account_links",
    "runtsuite_list_review_queue": "list_review_queue",
}


class RuntsuiteMCPServer:
    def __init__(self, adapter: RuntsuiteReadOnlyAdapter) -> None:
        self.adapter = adapter

    def list_tools(self) -> list[dict[str, Any]]:
        rows = []
        for tool in TOOLS:
            props = ({"external_member_id": {"type": "string", "minLength": 1, "maxLength": 240}} if tool == "runtsuite_get_member" else {})
            required = ["external_member_id"] if props else []
            rows.append(_tool(tool, f"Explicit read-only RUNTSuite capability: {tool}.", props, required))
        return rows

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        method = TOOLS.get(name)
        if not method:
            return _error("POLICY_DENIED")
        if name == "runtsuite_get_member":
            if set(arguments) != {"external_member_id"}:
                return _error("POLICY_DENIED")
            args = (str(arguments["external_member_id"]),)
        elif arguments:
            return _error("POLICY_DENIED")
        else:
            args = ()
        try:
            value = getattr(self.adapter, method)(*args)
            payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
            return _result({"ok": True, "result": payload})
        except Exception as exc:
            code = str(exc) if str(exc).startswith("runtsuite_") else "SOURCE_UNAVAILABLE"
            return _error(code.upper())


__all__ = ["RuntsuiteMCPServer", "TOOLS"]
