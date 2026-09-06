from __future__ import annotations

import json
from typing import Any, Mapping

from .pec_runts import AuthorityStatus, PecRuntsService, RuntsAuthRequired
from .platform import CapabilityDescriptor, CapabilityPermission, PromotionState


def _id() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": 240, "pattern": r"^[A-Za-z0-9_.:@/-]+$"}


TOOLS = {
    "pec_discover_messages": ({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
    "pec_get_message": ({"message_id": _id()}, ["message_id"]),
    "pec_list_attachments": ({"message_id": _id()}, ["message_id"]),
    "pec_find_runts_notifications": ({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
    "pec_find_by_runts_reference": ({"runts_reference": _id(), "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, ["runts_reference"]),
    "runts_sync_messages_practices": ({"limit": {"type": "integer", "minimum": 1, "maximum": 100}}, []),
    "runts_get_authoritative_for_pec": ({"pec_message_id": _id()}, ["pec_message_id"]),
    "runts_correlate_runtsuite": ({"practice_id": _id()}, ["practice_id"]),
    "runts_prepare_action": ({"practice_id": _id(), "action": {"type": "string", "pattern": r"^[A-Z][A-Z0-9_]{1,95}$"}, "reason": {"type": "string", "minLength": 1, "maxLength": 1000}}, ["practice_id", "action", "reason"]),
}


class PecRuntsMCPServer:
    def __init__(self, service: PecRuntsService) -> None:
        self.service = service

    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": name, "description": _description(name), "inputSchema": {"type": "object", "properties": schema, "required": required, "additionalProperties": False}} for name, (schema, required) in TOOLS.items()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        contract = TOOLS.get(name)
        if contract is None or not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")
        properties, required = contract
        if not set(arguments) <= set(properties) or not set(required) <= set(arguments):
            return _error("POLICY_DENIED")
        try:
            if name == "pec_discover_messages":
                data = {"messages": _dump(self.service.discover_pec(limit=int(arguments.get("limit", 100))))}
            elif name == "pec_get_message":
                data = {"message": self.service.get_pec(str(arguments["message_id"])).model_dump(mode="json")}
            elif name == "pec_list_attachments":
                data = {"attachments": _dump(self.service.get_pec(str(arguments["message_id"])).attachments)}
            elif name == "pec_find_runts_notifications":
                data = {"messages": _dump(self.service.find_runts_notifications(limit=int(arguments.get("limit", 100))))}
            elif name == "pec_find_by_runts_reference":
                data = {"messages": _dump(self.service.find_pec_by_runts_reference(str(arguments["runts_reference"]), limit=int(arguments.get("limit", 100))))}
            elif name == "runts_sync_messages_practices":
                data = {"items": _dump(self.service.sync_runts(limit=int(arguments.get("limit", 100))))}
            elif name == "runts_get_authoritative_for_pec":
                result = self.service.authoritative_for_pec(str(arguments["pec_message_id"]))
                if result.status is AuthorityStatus.AUTH_REQUIRED:
                    return _error("RUNTS_AUTH_REQUIRED", manual_action="Complete SPID/CIE authentication in the existing RUNTS browser tab; approve on phone if requested.")
                data = {"authority": result.model_dump(mode="json")}
            elif name == "runts_correlate_runtsuite":
                data = {"correlation": self.service.correlate_runtsuite(str(arguments["practice_id"])).model_dump(mode="json")}
            else:
                practice = self.service.runts.get_practice(str(arguments["practice_id"]))
                if practice.native_id != str(arguments["practice_id"]):
                    return _error("CONFLICT")
                proposal = self.service.prepare_action(practice.native_id, str(arguments["action"]), str(arguments["reason"]), (practice.source,))
                data = {"proposal": proposal.model_dump(mode="json"), "writes": 0}
            return _result(data)
        except RuntsAuthRequired:
            return _error("RUNTS_AUTH_REQUIRED", manual_action="Complete SPID/CIE authentication in the existing RUNTS browser tab; approve on phone if requested.")
        except ValueError:
            return _error("MALFORMED_RESPONSE")
        except Exception as exc:
            from .pec_browser_adapter import PecBrowserError
            if isinstance(exc, PecBrowserError):
                return _error(exc.status)
            return _error("SOURCE_UNAVAILABLE")


def capability_descriptors() -> tuple[CapabilityDescriptor, ...]:
    keyword_map = {
        "pec_discover_messages": ("pec", "posta certificata", "nuovi messaggi", "ricevuto"),
        "pec_get_message": ("pec", "messaggio", "leggi", "comunicazione"),
        "pec_list_attachments": ("pec", "allegati", "documenti"),
        "pec_find_runts_notifications": ("pec", "runts", "notifica", "notifiche", "trova", "collegate", "registro terzo settore"),
        "pec_find_by_runts_reference": ("pec", "runts", "pratica", "comunicazione", "riferimento", "cerca"),
        "runts_sync_messages_practices": ("runts", "pratiche", "messaggi", "aggiorna"),
        "runts_get_authoritative_for_pec": ("runts", "pec", "autoritativo", "comunicazione ufficiale"),
        "runts_correlate_runtsuite": ("runts", "runtsuite", "pratica", "correla", "review queue"),
        "runts_prepare_action": ("runts", "prepara", "azione", "risposta", "documento"),
    }
    rows = []
    for name, (schema, required) in TOOLS.items():
        permission = CapabilityPermission.PROPOSE if name == "runts_prepare_action" else CapabilityPermission.READ
        rows.append(CapabilityDescriptor(capability_id=name, server_id="pec_runts.mcp", domain="pec_runts", name=name, description=_description(name), keywords=keyword_map[name], input_schema={"type": "object", "properties": schema, "required": required, "additionalProperties": False}, permission=permission, approval_required=permission is CapabilityPermission.PROPOSE, source_system="pec" if name.startswith("pec_") else "runts", version="1", promotion=PromotionState.SHADOW, enabled=True, health="authenticated_read_or_auth_boundary"))
    return tuple(rows)


def _description(name: str) -> str:
    return f"Semantic PEC/RUNTS capability {name}; no generic request and no write execution."


def _dump(rows) -> list[dict[str, Any]]:
    return [row.model_dump(mode="json") for row in rows]


def _result(data: Mapping[str, Any]) -> dict[str, Any]:
    payload = {"ok": True, **dict(data), "writes": 0}
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "structuredContent": payload, "isError": False}


def _error(status: str, **extra: Any) -> dict[str, Any]:
    payload = {"ok": False, "status": status, **extra, "writes": 0}
    return {"content": [{"type": "text", "text": status}], "structuredContent": payload, "isError": True}


__all__ = ["PecRuntsMCPServer", "TOOLS", "capability_descriptors"]
