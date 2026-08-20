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
from src.arci import READ_TOOL
from src.mcp_transport import MCP_PROTOCOL_VERSION


EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
    "additionalProperties": False,
}
TOOLS = {READ_TOOL: EMPTY_SCHEMA}


class ArciMCPServer:
    def __init__(self, provider: ArciPortalReadOnly) -> None:
        self.provider = provider

    def list_tools(self) -> list[dict[str, Any]]:
        return [{
            "name": READ_TOOL,
            "description": (
                "Read PII-minimized ARCI organization facts and aggregate "
                "governance age buckets from the authenticated work profile."
            ),
            "inputSchema": dict(EMPTY_SCHEMA),
        }]

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if name != READ_TOOL or not isinstance(arguments, Mapping) or arguments:
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
