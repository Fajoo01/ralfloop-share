#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

import websocket


def load_config(path: str | Path) -> tuple[str, dict[str, str]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    site = str(payload.get("site") or "").strip().casefold()
    if not site:
        raise ValueError("missing_site")
    entities: dict[str, str] = {}
    for item in payload.get("entities") or []:
        if isinstance(item, str):
            entity_id = item.strip(); role = "primary_observation"
        elif isinstance(item, Mapping):
            entity_id = str(item.get("entity_id") or "").strip()
            role = str(item.get("role") or "primary_observation").strip()
        else:
            continue
        if entity_id:
            entities[entity_id] = role
    if not entities:
        raise ValueError("missing_entities")
    return site, entities


def websocket_url(ha_url: str) -> str:
    parsed = urlparse(ha_url.rstrip("/"))
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/api/websocket"


def sanitize_state_event(message: Mapping[str, Any], *, site: str, allowed: Mapping[str, str]) -> dict[str, Any] | None:
    if message.get("type") != "event":
        return None
    event = message.get("event")
    if not isinstance(event, Mapping) or event.get("event_type") != "state_changed":
        return None
    data = event.get("data")
    if not isinstance(data, Mapping):
        return None
    entity_id = str(data.get("entity_id") or "")
    if entity_id not in allowed:
        return None
    signal_role = str(allowed.get(entity_id) or "primary_observation")
    old = data.get("old_state") if isinstance(data.get("old_state"), Mapping) else {}
    new = data.get("new_state") if isinstance(data.get("new_state"), Mapping) else {}
    old_state = old.get("state")
    new_state = new.get("state")
    if old_state == new_state:
        return None
    ts = str(event.get("time_fired") or new.get("last_changed") or "")
    identity = json.dumps([site, entity_id, old_state, new_state, ts], separators=(",", ":"), default=str)
    return {
        "event_id": "ha_sede_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
        "ts": ts,
        "source": "home_assistant_ws",
        "event": "HA_STATE_CHANGED",
        "site": site,
        "entity_id": entity_id,
        "domain": entity_id.split(".", 1)[0],
        "signal_role": signal_role,
        "old_state": old_state,
        "new_state": new_state,
    }


def append_event(path: Path, row: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(dict(row), ensure_ascii=False, separators=(",", ":")) + "\n")


def run_once(*, ha_url: str, token: str, site: str, allowed: Mapping[str, str], out: Path) -> None:
    ws = websocket.create_connection(websocket_url(ha_url), timeout=30)
    try:
        hello = json.loads(ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError("ha_ws_auth_required_missing")
        ws.send(json.dumps({"type": "auth", "access_token": token}))
        auth = json.loads(ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError("ha_ws_auth_failed")
        ws.send(json.dumps({"id": 1, "type": "subscribe_events", "event_type": "state_changed"}))
        sub = json.loads(ws.recv())
        if sub.get("type") != "result" or sub.get("success") is not True:
            raise RuntimeError("ha_ws_subscribe_failed")
        while True:
            message = json.loads(ws.recv())
            row = sanitize_state_event(message, site=site, allowed=allowed)
            if row is not None:
                append_event(out, row)
    finally:
        ws.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/home/sibilla-cumana/ralf-memory-rag/current/config/sede_observation_entities.json")
    parser.add_argument("--out", default="/var/lib/bottazzi-sede-events/ha_events.jsonl")
    args = parser.parse_args()
    ha_url = os.environ.get("HA_URL", "").strip()
    token = os.environ.get("HA_TOKEN", "").strip()
    if not ha_url or not token:
        raise SystemExit("missing_home_assistant_environment")
    site, allowed = load_config(args.config)
    delay = 1.0
    while True:
        try:
            run_once(ha_url=ha_url, token=token, site=site, allowed=allowed, out=Path(args.out))
            delay = 1.0
        except KeyboardInterrupt:
            return 0
        except Exception as exc:
            print(json.dumps({"status":"reconnect","error":type(exc).__name__,"delay":delay}), flush=True)
            time.sleep(delay)
            delay = min(30.0, delay * 2.0)


if __name__ == "__main__":
    raise SystemExit(main())
