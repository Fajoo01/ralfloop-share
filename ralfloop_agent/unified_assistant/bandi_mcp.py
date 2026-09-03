from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from .bandi_service import BandiService, TiremmEligibilityProfile


class BandiMCPServer:
    def __init__(self, service: BandiService, profile: TiremmEligibilityProfile | None = None) -> None:
        self.service = service
        self.profile = profile or TiremmEligibilityProfile()

    def list_tools(self) -> list[dict[str, Any]]:
        return [{
            "name": name,
            "description": f"Semantic source-backed Bandi capability: {name}.",
            "inputSchema": {
                "type": "object", "properties": properties,
                "required": required, "additionalProperties": False,
            },
        } for name, (properties, required) in TOOLS.items()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS or not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")
        properties, required = TOOLS[name]
        if not set(arguments) <= set(properties) or not set(required) <= set(arguments):
            return _error("POLICY_DENIED")
        try:
            now = datetime.now(timezone.utc)
            if name == "bandi_search":
                rows = self.service.search(str(arguments["query"]), limit=int(arguments.get("limit", 20)))
                data = {"items": _dump(rows)}
            elif name == "bandi_list_open":
                data = {"items": _dump(self.service.list_open(now=now, limit=int(arguments.get("limit", 100))))}
            elif name == "bandi_find_for_tiremm":
                rows = self.service.list_open(now=now, limit=int(arguments.get("limit", 100)))
                data = {"items": [{
                    "bando": row.model_dump(mode="json"),
                    "eligibility": self.service.evaluate_observed(row, self.profile, now=now).model_dump(mode="json"),
                } for row in rows]}
            else:
                row = self.service.get(str(arguments["bando_id"]))
                if row is None:
                    return _error("NOT_FOUND")
                if name == "bandi_get":
                    data = {"bando": row.model_dump(mode="json")}
                elif name == "bandi_get_documents":
                    data = {"documents": _dump(row.attachments)}
                elif name == "bandi_get_deadlines":
                    data = {"opening_at": _iso(row.opening_at), "deadline_at": _iso(row.deadline_at), "status": row.status}
                elif name == "bandi_get_requirements":
                    data = {"requirements": row.requirements.model_dump(mode="json")}
                else:
                    data = {"events": _dump(self.service.changes(row.entity_id))}
            return _result(data)
        except (TypeError, ValueError):
            return _error("MALFORMED_RESPONSE")
        except Exception:
            return _error("SOURCE_UNAVAILABLE")


def _str(minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "string", "minLength": minimum, "maxLength": maximum}


def _id() -> dict[str, Any]:
    return {**_str(1, 240), "pattern": r"^bando\.[a-f0-9]{32}$"}


def _int(minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _dump(rows) -> list[dict[str, Any]]:
    return [row.model_dump(mode="json") for row in rows]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _result(data: Mapping[str, Any]) -> dict[str, Any]:
    payload = {"ok": True, **data, "writes": 0, "sends": 0}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload, "isError": False}


def _error(code: str) -> dict[str, Any]:
    payload = {"ok": False, "status": code, "writes": 0, "sends": 0}
    return {"content": [{"type": "text", "text": code}], "structuredContent": payload, "isError": True}


TOOLS = {
    "bandi_search": ({"query": _str(1, 500), "limit": _int(1, 100)}, ["query"]),
    "bandi_list_open": ({"limit": _int(1, 100)}, []),
    "bandi_get": ({"bando_id": _id()}, ["bando_id"]),
    "bandi_get_documents": ({"bando_id": _id()}, ["bando_id"]),
    "bandi_get_deadlines": ({"bando_id": _id()}, ["bando_id"]),
    "bandi_get_requirements": ({"bando_id": _id()}, ["bando_id"]),
    "bandi_get_changes": ({"bando_id": _id()}, ["bando_id"]),
    "bandi_find_for_tiremm": ({"limit": _int(1, 100)}, []),
}


__all__ = ["BandiMCPServer", "TOOLS"]
