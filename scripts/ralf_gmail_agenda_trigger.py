#!/usr/bin/env python3
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Any, Mapping

from ralfloop_agent.unified_assistant.agenda_ingress import build_default_agenda_intake
from ralfloop_agent.unified_assistant.email_send import GoogleWorkspaceEmailContext
from ralfloop_agent.unified_assistant.memory_service import MemoryService


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(value, dict):
        raise ValueError("gmail_agenda_state_invalid")
    return value


def _save(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(value), sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
    socket = os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
    timeout = float(os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "20"))
    state_path = Path(os.getenv("BOTTAZZI_GMAIL_AGENDA_STATE", str(Path.home() / ".local/state/ralf/gmail-agenda-trigger.json")))
    memory_path = Path(os.getenv("RALFLOOP_OPERATIONAL_MEMORY_PATH", str(Path.home() / ".local/share/bottazzi/runtime-production/operational-memory.sqlite3")))
    factory = lambda: GoogleWorkspaceEmailContext(socket, account, timeout)
    with factory() as gateway:
        result = gateway.invoke("search", query="in:inbox newer_than:2d", maxResults=50)
    rows = result.get("messages") if isinstance(result, Mapping) else []
    ids = [str(row.get("messageId") or "") for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []
    ids = [item for item in ids if item]
    state = _load(state_path)
    if state is None:
        _save(state_path, {"schema_version": "gmail_agenda_v1", "known_message_ids": ids, "activated_at": datetime.now(UTC).isoformat()})
        print(json.dumps({"bootstrapped": True, "ingested": 0, "seen": len(ids)}, sort_keys=True))
        return 0
    known = set(map(str, state.get("known_message_ids") or ()))
    ingested = 0
    with MemoryService(memory_path) as memory:
        intake = build_default_agenda_intake(memory)
        for message_id in ids:
            if message_id in known:
                continue
            with factory() as gateway:
                detail = gateway.invoke("read", messageId=message_id)
            message = detail.get("message") if isinstance(detail, Mapping) else None
            if isinstance(message, Mapping):
                if intake.ingest_email(message) is not None:
                    ingested += 1
            known.add(message_id)
    _save(state_path, {"schema_version": "gmail_agenda_v1", "known_message_ids": sorted(known)[-5000:], "activated_at": state.get("activated_at")})
    print(json.dumps({"bootstrapped": False, "ingested": ingested, "seen": len(ids)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
