#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.frontend_designer import FrontendDesigner, FrontendDesignerConfig
from ralfloop_agent.frontend_designer.mcp import MCP_PROTOCOL_VERSION, FrontendDesignerMCPServer


def build_server() -> FrontendDesignerMCPServer:
    config = FrontendDesignerConfig.from_environment(project_root=PROJECT_ROOT)
    return FrontendDesignerMCPServer(FrontendDesigner(config))


def _response(request: Mapping[str, Any], server: FrontendDesignerMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ralf-frontend-designer", "version": "1"},
            "instructions": (
                "Reusable frontend design and verification MCP. Work only inside configured worktree roots. "
                "For Android, emulator validation must pass before real-phone validation through Tiremm Remote."
            ),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": server.list_tools()}
    elif method == "tools/call":
        params = request.get("params") or {}
        if not isinstance(params, Mapping):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "invalid_params"}}
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, Mapping):
            return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "invalid_params"}}
        result = server.call(str(params.get("name") or ""), arguments)
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method_not_found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    server = build_server()
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            request = json.loads(raw)
            if not isinstance(request, Mapping):
                raise TypeError("request_not_object")
            response = _response(request, server)
        except (json.JSONDecodeError, TypeError, ValueError):
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse_error"}}
        except Exception:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal_error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
