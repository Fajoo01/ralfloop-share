#!/usr/bin/env python3
from __future__ import annotations

"""Semantic read-only MCP server for authenticated ARCI organization data."""

import json
from pathlib import Path
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.arci_portal import ArciPortalReadOnly
from ralfloop_agent.unified_assistant.arci_point_reads import (
    ArciPointReadError,
    ArciPointReadService,
)
from ralfloop_agent.unified_assistant.arci_datatables import (
    ArciDataTablesService,
    ArciListQuery,
)
from src.arci import READ_TOOL
from src.mcp_transport import MCP_PROTOCOL_VERSION


EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
ID_SCHEMA: dict[str, Any] = {
    "type": "string", "minLength": 1, "maxLength": 240,
    "pattern": r"^[A-Za-z0-9_.:-]+$",
}
POINT_TOOLS: dict[str, dict[str, Any]] = {
    "arci_get_member": {"user_id": ID_SCHEMA},
    "arci_get_card": {"card_id": ID_SCHEMA},
    "arci_list_member_cards": {"user_id": ID_SCHEMA},
    "arci_get_club": {"club_id": ID_SCHEMA},
    "arci_verify_membership": {"user_id": ID_SCHEMA, "club_id": ID_SCHEMA},
}
LIST_PROPERTIES: dict[str, Any] = {
    "club_id": ID_SCHEMA,
    "committee_id": {**ID_SCHEMA, "type": ["string", "null"]},
    "regional_id": {**ID_SCHEMA, "type": ["string", "null"]},
    "validity": {"type": ["integer", "null"], "minimum": 2000, "maximum": 2200},
    "search": {"type": ["string", "null"], "maxLength": 200},
}
COMPLETE_TOOLS = {
    "arci_list_members": LIST_PROPERTIES,
    "arci_list_cards": LIST_PROPERTIES,
    "arci_list_pending_card_requests": LIST_PROPERTIES,
}
TOOLS = {READ_TOOL: EMPTY_SCHEMA}


class ArciMCPServer:
    def __init__(
        self,
        provider: ArciPortalReadOnly,
        point_reads: ArciPointReadService | None = None,
        datatables: ArciDataTablesService | None = None,
    ) -> None:
        self.provider = provider
        self.point_reads = point_reads
        self.datatables = datatables

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [{
            "name": READ_TOOL,
            "description": (
                "Read PII-minimized ARCI organization facts and aggregate "
                "governance age buckets from the authenticated work profile."
            ),
            "inputSchema": dict(EMPTY_SCHEMA),
        }]
        if self.point_reads is not None:
            for name, properties in POINT_TOOLS.items():
                tools.append({
                    "name": name,
                    "description": f"Semantic read-only ARCI capability: {name}.",
                    "inputSchema": {
                        "type": "object",
                        "properties": properties,
                        "required": list(properties),
                        "additionalProperties": False,
                    },
                })
        if self.datatables is not None:
            for name, properties in COMPLETE_TOOLS.items():
                tools.append({
                    "name": name,
                    "description": f"Complete internally-paginated read-only ARCI capability: {name}.",
                    "inputSchema": {
                        "type": "object", "properties": properties,
                        "required": ["club_id"], "additionalProperties": False,
                    },
                })
        return tools

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")

        if name in POINT_TOOLS:
            if self.point_reads is None or set(arguments) != set(POINT_TOOLS[name]):
                return _error("POLICY_DENIED")
            try:
                method = getattr(self.point_reads, name.removeprefix("arci_"))
                result = method(**arguments)
                data = result.model_dump(mode="json") if hasattr(result, "model_dump") else [
                    row.model_dump(mode="json") for row in result
                ]
                return _success(name.removeprefix("arci_"), data)
            except ArciPointReadError as exc:
                return _error(exc.code.value)
            except Exception:
                return _error("SOURCE_UNAVAILABLE")

        if name in COMPLETE_TOOLS:
            if self.datatables is None or not set(arguments) <= set(COMPLETE_TOOLS[name]) or "club_id" not in arguments:
                return _error("POLICY_DENIED")
            try:
                query = ArciListQuery.model_validate(arguments)
                method = getattr(self.datatables, name.removeprefix("arci_"))
                return _success(name.removeprefix("arci_"), method(query).model_dump(mode="json"))
            except ArciPointReadError as exc:
                return _error(exc.code.value)
            except Exception:
                return _error("MALFORMED_RESPONSE")

        if name != READ_TOOL or arguments:
            return _error("POLICY_DENIED")

        try:
            profile = self.provider.read_organization_profile()
        except Exception:
            return _error("SOURCE_UNAVAILABLE")

        payload = {
            "ok": profile.status in {"FOUND", "PARTIAL"},
            "operation": "read_organization_profile",
            **profile.model_dump(mode="json"),
            "writes": 0,
            "sends": 0,
        }
        return {
            "content": [{
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False),
            }],
            "structuredContent": payload,
            "isError": not payload["ok"],
        }


def _error(code: str) -> dict[str, Any]:
    payload = {
        "ok": False,
        "status": code,
        "side_effects": 0,
        "writes": 0,
        "sends": 0,
    }
    return {
        "content": [{"type": "text", "text": code}],
        "structuredContent": payload,
        "isError": True,
    }


def _success(operation: str, data: Any) -> dict[str, Any]:
    payload = {
        "ok": True,
        "operation": operation,
        "data": data,
        "side_effects": 0,
        "writes": 0,
        "sends": 0,
    }
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": payload,
        "isError": False,
    }


def _response(
    request: Mapping[str, Any],
    server: ArciMCPServer,
) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None

    request_id = request.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ralf-arci-read", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": server.list_tools()}
    elif method == "tools/call":
        params = request.get("params")
        if not isinstance(params, Mapping):
            result = _error("POLICY_DENIED")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        arguments = params.get("arguments", {})
        result = server.call(
            str(params.get("name") or ""),
            arguments,
        )
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "method_not_found"},
        }

    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    server = ArciMCPServer(ArciPortalReadOnly.from_environment())
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise ValueError("request_not_object")
            response = _response(request, server)
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32603, "message": "internal_error"},
            }
        if response is not None:
            sys.stdout.write(
                json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
