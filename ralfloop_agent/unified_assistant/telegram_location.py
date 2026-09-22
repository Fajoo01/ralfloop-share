from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping


def _root() -> Path:
    return Path(
        os.getenv(
            "RALFLOOP_TELEGRAM_LOCATION_DIR",
            "/home/sibilla-cumana/.local/state/ralf/telegram-locations",
        )
    )


def _ttl() -> int:
    try:
        return max(
            60,
            int(os.getenv("RALFLOOP_TELEGRAM_LOCATION_TTL_SEC", "7200")),
        )
    except ValueError:
        return 7200


def _ids(message: Mapping[str, Any]) -> tuple[int, int]:
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    return int(chat.get("id") or 0), int(sender.get("id") or 0)


def _path(chat_id: int, user_id: int) -> Path:
    return _root() / f"{chat_id}-{user_id}.json"


def remember_telegram_location(message: Mapping[str, Any]) -> bool:
    loc = message.get("location") or {}
    if "latitude" not in loc or "longitude" not in loc:
        return False

    lat = float(loc["latitude"])
    lon = float(loc["longitude"])

    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False

    chat_id, user_id = _ids(message)
    if not chat_id:
        return False

    root = _root()
    root.mkdir(parents=True, exist_ok=True)

    payload = {
        "lat": lat,
        "lon": lon,
        "chat_id": chat_id,
        "user_id": user_id,
        "saved_at": int(time.time()),
    }

    for uid in {user_id, 0}:
        path = _path(chat_id, uid)
        path.write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        try:
            path.chmod(0o600)
        except OSError:
            pass

    return True


def load_telegram_location(
    chat_id: int,
    user_id: int = 0,
) -> dict[str, Any] | None:
    candidates = (
        _path(chat_id, user_id),
        _path(chat_id, 0),
    )

    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue

        saved_at = int(payload.get("saved_at") or 0)
        if int(time.time()) - saved_at > _ttl():
            try:
                path.unlink()
            except OSError:
                pass
            continue

        try:
            lat = float(payload["lat"])
            lon = float(payload["lon"])
        except (KeyError, TypeError, ValueError):
            continue

        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return {
                "lat": lat,
                "lon": lon,
                "saved_at": saved_at,
                "source": "telegram_gps_cache",
            }

    return None
