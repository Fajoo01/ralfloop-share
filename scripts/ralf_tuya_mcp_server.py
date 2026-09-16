#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp_transport import MCP_PROTOCOL_VERSION
from ralfloop_agent.unified_assistant.tuya_mcp import TuyaMCPServer


def main() -> int:
    server = TuyaMCPServer()
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                result = {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "bottazzi-tuya", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": server.list_tools()}
            elif method == "tools/call":
                params = request.get("params") or {}
                result = server.call(str(params.get("name") or ""), params.get("arguments") or {})
            else:
                raise ValueError("method_not_found")
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "internal_error"},
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
