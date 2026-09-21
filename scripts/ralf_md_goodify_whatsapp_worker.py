#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.md_goodify import parse_md_goodify_qr
from ralfloop_agent.unified_assistant.md_goodify_transactional import MdGoodifyFlow

RECENT_FILE = Path(os.getenv("RALFLOOP_WHATSAPP_RECENT_FILE", "/var/lib/ralf-whatsapp-baileys/recent-messages.json"))
MEDIA_DIR = Path(os.getenv("RALFLOOP_WHATSAPP_MEDIA_DIR", "/var/lib/ralf-whatsapp-baileys/recent-media"))
STATE_FILE = Path(os.getenv("RALFLOOP_MD_GOODIFY_WHATSAPP_STATE", "/var/lib/ralfloop/md-goodify/whatsapp-qr-state.json"))
TARGET_JID = os.getenv("RALFLOOP_MD_GOODIFY_WHATSAPP_JID", "").strip()
POLL_SECONDS = max(2.0, float(os.getenv("RALFLOOP_MD_GOODIFY_WHATSAPP_POLL_SEC", "5")))
BACKFILL_SECONDS = max(0, int(os.getenv("RALFLOOP_MD_GOODIFY_WHATSAPP_BACKFILL_SEC", "86400")))


def _load_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def _save_state(value: Mapping[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(value), ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, STATE_FILE)


def _initial_state() -> dict[str, Any]:
    now_ms = int(time.time() * 1000)
    return {
        "version": 1,
        "started_at_ms": now_ms - BACKFILL_SECONDS * 1000,
        "processed": {},
        "updated_at": int(time.time()),
    }


def _recent_messages() -> list[dict[str, Any]]:
    value = _load_json(RECENT_FILE, {"messages": []})
    rows = value.get("messages") if isinstance(value, Mapping) else []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _media_path(message_id: str) -> Path:
    return MEDIA_DIR / f"{message_id}.bin"


def _decode_md_qr(path: Path):
    completed = subprocess.run(
        ["/usr/bin/zbarimg", "--quiet", "--raw", str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
        check=False,
    )
    if completed.returncode not in (0, 4) or not completed.stdout.strip():
        return None
    for raw in completed.stdout.splitlines():
        candidate = raw.strip()
        if not candidate:
            continue
        try:
            return parse_md_goodify_qr(candidate)
        except ValueError:
            continue
    return None


def _record(message: Mapping[str, Any], status: str, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "timestamp_ms": int(message.get("timestamp_ms") or 0),
        "media_sha256": extra.pop("media_sha256", ""),
        "processed_at": int(time.time()),
        **extra,
    }


def run_once(flow: MdGoodifyFlow | None = None) -> dict[str, Any]:
    if not TARGET_JID:
        raise RuntimeError("RALFLOOP_MD_GOODIFY_WHATSAPP_JID_required")
    flow = flow or MdGoodifyFlow()
    state = _load_json(STATE_FILE, None)
    if not isinstance(state, Mapping):
        state = _initial_state()
    else:
        state = dict(state)
    processed = dict(state.get("processed") or {})
    cutoff = int(state.get("started_at_ms") or 0)
    seen_now = 0
    qr_now = 0
    donated_now = 0

    rows = sorted(_recent_messages(), key=lambda row: int(row.get("timestamp_ms") or 0))
    for message in rows:
        message_id = str(message.get("message_id") or "")
        if not message_id or message_id in processed:
            continue
        if str(message.get("jid") or "") != TARGET_JID:
            continue
        if str(message.get("kind") or "") != "image":
            continue
        if int(message.get("timestamp_ms") or 0) < cutoff:
            continue
        media_path = _media_path(message_id)
        if not media_path.is_file():
            continue
        seen_now += 1
        try:
            content = media_path.read_bytes()
        except OSError:
            continue
        media_sha = hashlib.sha256(content).hexdigest()
        qr = _decode_md_qr(media_path)
        if qr is None:
            processed[message_id] = _record(message, "NO_MD_QR", media_sha256=media_sha)
            continue
        qr_now += 1
        try:
            result = flow.process_qr(qr.payload)
        except Exception as exc:
            processed[message_id] = _record(
                message, "FLOW_ERROR", media_sha256=media_sha,
                error_class=type(exc).__name__,
            )
            continue
        status = str(result.get("status") or "")
        if status == "DONATED_TO_TIREMM":
            donated_now += 1
        processed[message_id] = _record(
            message, status or "UNKNOWN", media_sha256=media_sha,
            donation_id=str(result.get("donation_id") or ""),
            already_processed=bool(result.get("already_processed")),
        )

    if len(processed) > 5000:
        ordered = sorted(
            processed.items(), key=lambda item: int((item[1] or {}).get("processed_at") or 0)
        )
        processed = dict(ordered[-5000:])
    state["processed"] = processed
    state["updated_at"] = int(time.time())
    _save_state(state)
    return {
        "ok": True,
        "media_seen_now": seen_now,
        "qr_found_now": qr_now,
        "donated_now": donated_now,
        "processed_total": len(processed),
    }


def main() -> int:
    once = "--once" in sys.argv[1:]
    while True:
        try:
            result = run_once()
            if once or result["media_seen_now"] or result["qr_found_now"]:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error_class": type(exc).__name__}), file=sys.stderr, flush=True)
            if once:
                return 1
        if once:
            return 0
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
