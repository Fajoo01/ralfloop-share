#!/usr/bin/env python3
from __future__ import annotations

"""Read-only MCP server for the Tiremm Innanz PEC mailbox."""

import base64
import json
from pathlib import Path
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.pec_provider_factory import build_default_pec_provider
from src.mcp_transport import MCP_PROTOCOL_VERSION

LIMIT = {"type": "integer", "minimum": 1, "maximum": 100}
ID = {"type": "string", "minLength": 1, "maxLength": 240}
QUERY = {"type": "string", "minLength": 1, "maxLength": 500}
TOOLS: dict[str, tuple[dict[str, Any], list[str]]] = {
    "pec_discover_messages": ({"limit": LIMIT}, []),
    "pec_get_message": ({"message_id": ID}, ["message_id"]),
    "pec_list_attachments": ({"message_id": ID}, ["message_id"]),
    "pec_get_attachment": ({"message_id": ID, "attachment_id": ID}, ["message_id", "attachment_id"]),
    "pec_search_messages": ({"query": QUERY, "limit": LIMIT}, ["query"]),
}


class PecMCPServer:
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    def list_tools(self) -> list[dict[str, Any]]:
        descriptions = {
            "pec_discover_messages": "List recent PEC messages without changing read/unread state.",
            "pec_get_message": "Read one PEC message by its exact native ID without mutation.",
            "pec_list_attachments": "List metadata for attachments of one PEC message.",
            "pec_get_attachment": "Read one PEC attachment as base64 without modifying the mailbox.",
            "pec_search_messages": "Search recent PEC sender, subject and body text without mutation.",
        }
        return [
            {
                "name": name,
                "description": descriptions[name],
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            }
            for name, (properties, required) in TOOLS.items()
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        contract = TOOLS.get(name)
        if contract is None or not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")
        properties, required = contract
        if not set(arguments) <= set(properties) or not set(required) <= set(arguments):
            return _error("POLICY_DENIED")
        try:
            limit = int(arguments.get("limit", 100))
            if not 1 <= limit <= 100:
                return _error("POLICY_DENIED")
            if name == "pec_discover_messages":
                rows = self.provider.list_messages(limit=limit)
                return _success("messages", [_dump(row) for row in rows])
            if name == "pec_get_message":
                message_id = str(arguments["message_id"])
                row = self.provider.get_message(message_id)
                if str(getattr(row, "native_id", "")) != message_id:
                    return _error("IDENTITY_MISMATCH")
                return _success("message", _dump(row))
            if name == "pec_list_attachments":
                message_id = str(arguments["message_id"])
                row = self.provider.get_message(message_id)
                if str(getattr(row, "native_id", "")) != message_id:
                    return _error("IDENTITY_MISMATCH")
                return _success("attachments", [_dump(item) for item in getattr(row, "attachments", ())])
            if name == "pec_get_attachment":
                message_id = str(arguments["message_id"])
                attachment_id = str(arguments["attachment_id"])
                method = getattr(self.provider, "download_attachment", None)
                if not callable(method):
                    return _error("ATTACHMENT_READ_UNAVAILABLE")
                data = method(message_id, attachment_id)
                return _success("attachment", {
                    "message_id": message_id,
                    "attachment_id": attachment_id,
                    "size": len(data),
                    "data_base64": base64.b64encode(data).decode("ascii"),
                })
            query = str(arguments["query"]).strip().casefold()
            if not query:
                return _error("POLICY_DENIED")
            rows = self.provider.list_messages(limit=limit)
            matches = []
            for row in rows:
                haystack = "\n".join(
                    str(getattr(row, field, "") or "")
                    for field in ("sender", "subject", "body", "runts_reference")
                ).casefold()
                if query in haystack:
                    matches.append(_dump(row))
            return _success("messages", matches)
        except Exception as exc:
            status = getattr(exc, "status", None) or getattr(exc, "code", None)
            return _error(str(status or "SOURCE_UNAVAILABLE"))


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return dict(value)
    return dict(vars(value))


def _success(key: str, value: Any) -> dict[str, Any]:
    payload = {"ok": True, key: value, "writes": 0, "sends": 0}
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, default=str)}],
        "structuredContent": payload,
        "isError": False,
    }


def _error(status: str) -> dict[str, Any]:
    payload = {"ok": False, "status": status, "writes": 0, "sends": 0}
    return {
        "content": [{"type": "text", "text": status}],
        "structuredContent": payload,
        "isError": True,
    }


def _response(request: Mapping[str, Any], server: PecMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ralf-pec-read", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": server.list_tools()}
    elif method == "tools/call":
        params = request.get("params")
        if not isinstance(params, Mapping):
            result = _error("POLICY_DENIED")
        else:
            arguments = params.get("arguments", {})
            result = server.call(str(params.get("name") or ""), arguments)
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "method_not_found"},
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    provider = build_default_pec_provider()
    server = PecMCPServer(provider)
    try:
        for line in sys.stdin:
            try:
                request = json.loads(line)
                if not isinstance(request, Mapping):
                    raise ValueError("request_not_object")
                response = _response(request, server)
            except Exception:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal_error"}}
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
                sys.stdout.flush()
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
