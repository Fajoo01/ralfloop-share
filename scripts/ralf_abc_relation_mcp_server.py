#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.abc_relation.mcp import MCP_PROTOCOL_VERSION, RelationMCPServer
from ralfloop_agent.abc_relation.service import RelationService
from ralfloop_agent.abc_relation.store import RelationStore


def _truthy(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


def build_server() -> RelationMCPServer:
    db_path = Path(
        os.environ.get(
            "ABC_RELATION_DB",
            str(Path.home() / ".local" / "share" / "ralfloop" / "abc_relation.sqlite3"),
        )
    ).expanduser()
    legacy = os.environ.get("ABC_LEGACY_REPORT", "").strip() or None
    store = RelationStore(db_path)
    service = RelationService(store, legacy_report_path=legacy)
    return RelationMCPServer(service, allow_local_writes=_truthy("ABC_MCP_ALLOW_LOCAL_WRITES"))


def _response(request: Mapping[str, Any], server: RelationMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "ralf-abc-relation", "version": "1"},
            "instructions": (
                "Use observable evidence first. Keep facts and interpretations separate. "
                "Psychology/dialogue frameworks are heuristics, never proof of hidden mental states. "
                "This server has no outbound messaging or surveillance tools."
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
        result = server.call(str(params.get("name") or ""), params.get("arguments") or {})
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
