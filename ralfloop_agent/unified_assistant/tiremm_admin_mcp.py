from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .service_identity_mcp import _error, _result, _tool
from .tiremm_admin import ActionProposal, TiremmAdminV2


TOOLS: dict[str, tuple[dict[str, Any], list[str]]] = {
    "tiremm_list_open_practices": ({}, []),
    "tiremm_get_practice": ({"practice_id": {"type": "string", "minLength": 1, "maxLength": 96}}, ["practice_id"]),
    "tiremm_get_deadlines": ({"now": {"type": "string", "format": "date-time"}, "within_days": {"type": "integer", "minimum": 0, "maximum": 3650}}, ["now"]),
    "tiremm_get_blocked": ({}, []),
    "tiremm_get_waiting": ({}, []),
    "tiremm_get_next_actions": ({}, []),
    "tiremm_get_conflicts": ({"practice_id": {"type": "string", "minLength": 1, "maxLength": 96}}, ["practice_id"]),
    "tiremm_get_sources": ({"practice_id": {"type": "string", "minLength": 1, "maxLength": 96}}, ["practice_id"]),
    "tiremm_get_timeline": ({"practice_id": {"type": "string", "minLength": 1, "maxLength": 96}}, ["practice_id"]),
    "tiremm_prepare_action": ({"proposal": {"type": "object"}}, ["proposal"]),
}


class TiremmAdminMCPServer:
    def __init__(self, service: TiremmAdminV2) -> None:
        self.service = service

    def list_tools(self) -> list[dict[str, Any]]:
        return [_tool(name, f"Semantic Tiremm Admin v2 capability: {name}.", props, required) for name, (props, required) in TOOLS.items()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        spec = TOOLS.get(name)
        if spec is None:
            return _error("FORBIDDEN")
        props, required = spec
        if not isinstance(arguments, Mapping) or set(arguments) - props.keys() or set(required) - arguments.keys():
            return _error("MALFORMED_RESPONSE")
        try:
            value = self._invoke(name, arguments)
            return _result({"ok": True, "result": _dump(value)})
        except KeyError:
            return _error("NOT_FOUND")
        except (TypeError, ValueError):
            return _error("MALFORMED_RESPONSE")

    def _invoke(self, name: str, arguments: Mapping[str, Any]) -> object:
        if name == "tiremm_list_open_practices":
            return self.service.list_open_practices()
        if name == "tiremm_get_practice":
            value = self.service.get_practice(str(arguments["practice_id"]))
            if value is None:
                raise KeyError("practice_missing")
            return value
        if name == "tiremm_get_deadlines":
            now = datetime.fromisoformat(str(arguments["now"]).replace("Z", "+00:00"))
            if now.tzinfo is None:
                raise ValueError("timezone_required")
            within = timedelta(days=int(arguments["within_days"])) if "within_days" in arguments else None
            return self.service.get_deadlines(now=now.astimezone(timezone.utc), within=within)
        if name == "tiremm_get_blocked":
            return self.service.get_blocked()
        if name == "tiremm_get_waiting":
            return self.service.get_waiting()
        if name == "tiremm_get_next_actions":
            return ({"practice_id": identity, "action": action.model_dump(mode="json")} for identity, action in self.service.get_next_actions())
        if name == "tiremm_get_conflicts":
            return self.service.get_conflicts(str(arguments["practice_id"]))
        if name == "tiremm_get_sources":
            return self.service.get_sources(str(arguments["practice_id"]))
        if name == "tiremm_get_timeline":
            return self.service.get_timeline(str(arguments["practice_id"]))
        return self.service.prepare_action(ActionProposal.model_validate(arguments["proposal"]))


def _dump(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list)) or not isinstance(value, (str, bytes, dict)) and hasattr(value, "__iter__"):
        return [_dump(row) for row in value]
    return value


__all__ = ["TOOLS", "TiremmAdminMCPServer"]
