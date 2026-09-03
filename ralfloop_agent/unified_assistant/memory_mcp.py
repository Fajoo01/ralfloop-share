from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .memory_service import MemoryService
from .service_identity_mcp import _error, _result, _tool


TOOLS = {
    "memory_get_practice": ({"practice_id": {"type": "string", "minLength": 1, "maxLength": 96}}, ["practice_id"]),
    "memory_search_documents": ({"query": {"type": "string", "minLength": 1, "maxLength": 1000}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["query"]),
    "memory_get_timeline": ({"entity_ref": {"type": "string", "minLength": 1, "maxLength": 240}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["entity_ref"]),
    "memory_get_open_practices": ({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
}


class MemoryMCPServer:
    def __init__(self, service: MemoryService) -> None:
        self.service = service

    def list_tools(self) -> list[dict[str, Any]]:
        return [_tool(name, f"Read-only semantic Memory Service capability: {name}.", props, required) for name, (props, required) in TOOLS.items()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS or not isinstance(arguments, Mapping):
            return _error("FORBIDDEN")
        props, required = TOOLS[name]
        if set(arguments) - props.keys() or set(required) - arguments.keys():
            return _error("MALFORMED_RESPONSE")
        try:
            limit = int(arguments.get("limit", 20))
            if name == "memory_get_practice":
                practice = self.service.get_practice(str(arguments["practice_id"]))
                if practice is None:
                    return _error("NOT_FOUND")
                value: object = practice.model_dump(mode="json")
            elif name == "memory_search_documents":
                value = [row.model_dump(mode="json") for row in self.service.search_documents(str(arguments["query"]), limit=limit)]
            elif name == "memory_get_timeline":
                value = [row.model_dump(mode="json") for row in self.service.timeline(str(arguments["entity_ref"]), limit=limit)]
            else:
                value = [row.model_dump(mode="json") for row in self.service.list_practices(status="open", limit=limit)]
            return _result({"ok": True, "result": value})
        except (TypeError, ValueError):
            return _error("MALFORMED_RESPONSE")
        except sqlite3.Error:
            return _error("SOURCE_UNAVAILABLE")

__all__ = ["MemoryMCPServer", "TOOLS"]
