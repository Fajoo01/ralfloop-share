"""Strict Unix MCP adapter for the reusable frontend designer."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

FRONTEND_TOOLS = frozenset({
    "frontend_inspect_project",
    "frontend_design_contract",
    "frontend_design_apply",
    "frontend_test_web",
    "frontend_emulator_status",
    "frontend_test_android_emulator",
    "frontend_test_android",
    "frontend_test_android_phone",
})


def _payload(result: Mapping[str, Any]) -> Any:
    if result.get("isError"):
        raise MCPProtocolError("frontend_designer_tool_error")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        if set(structured) == {"ok", "result"}:
            return structured["result"]
        return structured
    content = result.get("content") or ()
    if content and isinstance(content[0], Mapping):
        text = content[0].get("text")
        if isinstance(text, str):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return result


def _workdir_from_objective(objective: str) -> str:
    match = re.search(r"(?<![A-Za-z0-9])(?P<path>/(?:[^\s'\"]+))", objective)
    return match.group("path").rstrip(".,;:") if match else ""


class FrontendDesignerMCPContext:
    def __init__(self, socket_path: str = "/run/ralf-frontend-designer-mcp/mcp.sock", timeout: float = 120.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "FrontendDesignerMCPContext":
        return cls(os.getenv("RALF_FRONTEND_DESIGNER_MCP_SOCKET", "/run/ralf-frontend-designer-mcp/mcp.sock"))

    def __enter__(self) -> "FrontendDesignerMCPContext":
        self.session = MCPClientSession(
            UnixMCPTransport(self.socket_path),
            timeout=self.timeout,
            client_name="bot-tazzi-frontend-designer",
        )
        self.session.__enter__()
        found = {tool.name for tool in self.session.list_tools()}
        if found != FRONTEND_TOOLS:
            self.session.close()
            self.session = None
            raise MCPProtocolError("frontend_designer_tool_allowlist_mismatch")
        return self

    def __exit__(self, *args: object) -> None:
        if self.session is not None:
            self.session.__exit__(*args)
            self.session = None

    def payload(self, name: str, arguments: Mapping[str, Any]) -> Any:
        if self.session is None or name not in FRONTEND_TOOLS:
            raise MCPProtocolError("frontend_designer_tool_not_available")
        return _payload(self.session.call_tool(name, arguments))

    def request(self, objective: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values = dict(arguments or {})
        lowered = objective.casefold()
        operation = str(values.get("operation") or "").strip().casefold()
        workdir = str(values.get("workdir") or _workdir_from_objective(objective)).strip()
        target = str(values.get("target") or "auto").strip().casefold()
        brief = str(values.get("brief") or objective).strip()

        if not operation:
            if values.get("url") or re.search(r"\b(?:screenshot|viewport|responsive\s+test|test\s+web)\b", lowered):
                operation = "test_web"
            elif values.get("package") or re.search(r"\b(?:apk|android\s+test|test\s+android|emulatore)\b", lowered):
                operation = "test_android"
            elif re.search(r"\b(?:applica|implementa|modifica|ridisegna|sistema|correggi|rifai)\b", lowered):
                operation = "apply"
            elif re.search(r"\b(?:contratto|specifica|spec|design|progetta|ux|ui)\b", lowered):
                operation = "contract"
            else:
                operation = "inspect"

        if operation == "emulator_status":
            result = self.payload("frontend_emulator_status", {})
        elif operation == "test_web":
            url = str(values.get("url") or "").strip()
            if not url:
                return {"status": "clarification_required", "error": "frontend_url_required"}
            result = self.payload("frontend_test_web", {"url": url})
        else:
            if not workdir:
                return {"status": "clarification_required", "error": "frontend_workdir_required"}
            if operation == "inspect":
                result = self.payload("frontend_inspect_project", {"workdir": workdir})
            elif operation == "contract":
                result = self.payload("frontend_design_contract", {"workdir": workdir, "brief": brief, "target": target})
            elif operation == "apply":
                result = self.payload("frontend_design_apply", {"workdir": workdir, "brief": brief, "target": target})
            elif operation == "test_android":
                package = str(values.get("package") or "").strip()
                if not package:
                    return {"status": "clarification_required", "error": "frontend_android_package_required"}
                call_args: dict[str, Any] = {"workdir": workdir, "package": package, "use_phone": bool(values.get("use_phone", False))}
                for key in ("apk_path", "phone_device_id"):
                    if values.get(key):
                        call_args[key] = values[key]
                result = self.payload("frontend_test_android", call_args)
            else:
                return {"status": "clarification_required", "error": "frontend_operation_unresolved"}

        return {
            "status": "completed" if not isinstance(result, Mapping) or result.get("ok", True) else "failed",
            "operation": operation,
            "result": result,
            "evidence_refs": [str(result.get("artifacts"))] if isinstance(result, Mapping) and result.get("artifacts") else [],
        }


__all__ = ["FRONTEND_TOOLS", "FrontendDesignerMCPContext"]
