#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, UnixMCPTransport


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def write_state(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def fetch_health(socket_path: str) -> dict[str, Any]:
    with MCPClientSession(
        UnixMCPTransport(socket_path, connect_timeout=1.0),
        timeout=8.0,
        client_name="ralf-domotics-watchdog",
    ) as client:
        names = {tool.name for tool in client.list_tools()}
        if "tuya_health" not in names:
            raise RuntimeError("tuya_health_not_discovered")
        result = client.call_tool("tuya_health", {})
    payload = result.get("structuredContent")
    if not isinstance(payload, Mapping) or payload.get("ok") is False:
        raise RuntimeError("tuya_health_malformed")
    return dict(payload)


def entity_ids(rows: Any) -> set[str]:
    if not isinstance(rows, list):
        return set()
    return {
        str(row.get("entity_id"))
        for row in rows
        if isinstance(row, Mapping) and row.get("entity_id")
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/run/ralf-tuya-mcp/mcp.sock")
    parser.add_argument("--state", default="/var/lib/ralf-domotics-watchdog/state.json")
    args = parser.parse_args()
    state_path = Path(args.state)
    previous = load_state(state_path)
    health = fetch_health(args.socket)
    state_health = health.get("state_health") or {}
    current_unavailable = entity_ids(state_health.get("unavailable_entities"))
    current_missing = entity_ids(state_health.get("missing_entities"))
    previous_unavailable = set(previous.get("unavailable_entities") or [])
    previous_missing = set(previous.get("missing_entities") or [])
    baseline = not bool(previous.get("initialized"))
    event = {
        "initialized": True,
        "status": "baseline_initialized" if baseline else "steady",
        "availability": health.get("availability"),
        "counts": {
            key: int(state_health.get(key) or 0)
            for key in ("available", "unavailable", "unknown", "missing")
        },
        "new_unavailable": [] if baseline else sorted(current_unavailable - previous_unavailable),
        "recovered": [] if baseline else sorted(previous_unavailable - current_unavailable),
        "new_missing": [] if baseline else sorted(current_missing - previous_missing),
        "returned_missing": [] if baseline else sorted(previous_missing - current_missing),
        "unavailable_entities": sorted(current_unavailable),
        "missing_entities": sorted(current_missing),
        "degraded_devices": state_health.get("degraded_devices") or [],
    }
    if not baseline and any(event[key] for key in ("new_unavailable", "recovered", "new_missing", "returned_missing")):
        event["status"] = "changed"
    write_state(state_path, event)
    print(json.dumps(event, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
