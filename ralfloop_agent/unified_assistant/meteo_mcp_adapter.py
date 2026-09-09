from __future__ import annotations

import json
import os
import re
import socket
from typing import Any, Mapping

from .telegram_location import load_telegram_location


_RADAR_RE = re.compile(r"\bradar\b", re.I)

_LOCAL_WORDS = {
    "qui",
    "qua",
    "qui vicino",
    "da me",
    "adesso",
    "oggi",
}

_ADDRESS_PATTERNS = (
    re.compile(
        r"^\s*(?:meteo|weather|previsioni(?:\s+meteo)?|radar)"
        r"\s+(?:(?:a|in|per|su)\s+)?(?P<address>.+?)\s*[?!.]*$",
        re.I,
    ),
    re.compile(
        r"^\s*(?:che\s+tempo\s+fa|piove|piover[àa]|sta\s+piovendo)"
        r"\s+(?:a|in|per)\s+(?P<address>.+?)\s*[?!.]*$",
        re.I,
    ),
    re.compile(
        r"^\s*temperatura\s+(?:a|in|per)\s+(?P<address>.+?)\s*[?!.]*$",
        re.I,
    ),
)


def _extract_address(text: str) -> str | None:
    raw = text.strip()

    if re.search(
        r"\b(?:qui|qua|da\s+me)\b",
        raw,
        re.I,
    ):
        return None

    if re.search(r"\btemporale\s+in\s+arrivo\b", raw, re.I):
        return None

    for pattern in _ADDRESS_PATTERNS:
        match = pattern.match(raw)
        if not match:
            continue

        value = match.group("address").strip(" ,.?!" )
        if value.casefold() in _LOCAL_WORDS:
            return None
        if len(value) >= 2:
            return value

    return None


def _number(value: Any, digits: int = 1) -> str:
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return "?"


def _render_current(payload: Mapping[str, Any]) -> str:
    current = dict(payload.get("current") or {})
    location = str(payload.get("location") or "posizione indicata")

    lines = [
        f"Meteo per {location}.",
        (
            f"Adesso: {_number(current.get('temperature_2m'))} °C"
            f" (percepita {_number(current.get('apparent_temperature'))} °C), "
            f"precipitazioni {_number(current.get('precipitation'))} mm, "
            f"vento {_number(current.get('wind_speed_10m'))} km/h."
        ),
    ]

    future = list(payload.get("next_hours") or ())
    if future:
        rows = []
        for item in future[:6]:
            when = str(item.get("time") or "")
            clock = when[11:16] if len(when) >= 16 else when
            prob = item.get("precipitation_probability")
            mm = item.get("precipitation_mm")
            rows.append(
                f"{clock}: {prob if prob is not None else '?'}%, "
                f"{_number(mm)} mm"
            )
        lines.append("Prossime ore: " + "; ".join(rows) + ".")

    radar = payload.get("radar_url")
    if radar:
        lines.append(f"Radar: {radar}")

    return "\n".join(lines)


def _render_radar(payload: Mapping[str, Any]) -> str:
    location = str(payload.get("location") or "posizione indicata")
    url = str(payload.get("radar_url") or "")
    if not url:
        return "Radar non disponibile."
    return f"Radar meteo per {location}:\n{url}"


class MeteoMCPReadOnly:
    def __init__(self, context: Mapping[str, Any] | None = None) -> None:
        self.context = dict(context or {})
        self.socket_path = os.getenv(
            "RALF_METEO_MCP_SOCKET",
            "/run/ralf-meteo-mcp/mcp.sock",
        )

    def _direct_location(self) -> dict[str, float] | None:
        context = self.context

        candidates: list[Mapping[str, Any]] = []

        for key in (
            "telegram_location",
            "telegram_gps",
            "location",
        ):
            value = context.get(key)
            if isinstance(value, Mapping):
                candidates.append(value)

        message = context.get("message")
        if isinstance(message, Mapping):
            value = message.get("location")
            if isinstance(value, Mapping):
                candidates.append(value)

        telegram_message = context.get("telegram_message")
        if isinstance(telegram_message, Mapping):
            value = telegram_message.get("location")
            if isinstance(value, Mapping):
                candidates.append(value)

        candidates.append(context)

        for value in candidates:
            lat = value.get("latitude", value.get("lat"))
            lon = value.get("longitude", value.get("lon"))
            if lat is None or lon is None:
                continue
            try:
                lat_f = float(lat)
                lon_f = float(lon)
            except (TypeError, ValueError):
                continue
            if -90 <= lat_f <= 90 and -180 <= lon_f <= 180:
                return {"lat": lat_f, "lon": lon_f}

        return None

    def _cached_location(self) -> dict[str, Any] | None:
        try:
            chat_id = int(self.context.get("telegram_chat_id") or 0)
            user_id = int(self.context.get("telegram_user_id") or 0)
        except (TypeError, ValueError):
            return None

        if not chat_id:
            return None

        return load_telegram_location(chat_id, user_id)

    @staticmethod
    def _send(
        stream,
        request_id: int,
        method: str,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params or {}),
        }
        stream.write(
            (json.dumps(request, ensure_ascii=False) + "\n").encode()
        )
        stream.flush()

        line = stream.readline()
        if not line:
            raise RuntimeError("meteo_mcp_eof")

        response = json.loads(line.decode())
        if response.get("error"):
            raise RuntimeError(
                str(response["error"].get("message") or "meteo_mcp_error")
            )
        return dict(response.get("result") or {})

    def _call(
        self,
        tool: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(40)
            client.connect(self.socket_path)

            stream = client.makefile("rwb", buffering=0)

            self._send(stream, 1, "initialize", {})

            discovered = self._send(
                stream,
                2,
                "tools/list",
                {},
            )
            names = {
                str(item.get("name") or "")
                for item in discovered.get("tools") or ()
                if isinstance(item, Mapping)
            }
            if tool not in names:
                raise RuntimeError("meteo_tool_not_discovered")

            result = self._send(
                stream,
                3,
                "tools/call",
                {
                    "name": tool,
                    "arguments": dict(arguments),
                },
            )

        structured = result.get("structuredContent")
        if isinstance(structured, Mapping):
            return dict(structured)

        content = result.get("content") or ()
        if content and isinstance(content[0], Mapping):
            raw = content[0].get("text")
            if isinstance(raw, str):
                decoded = json.loads(raw)
                if isinstance(decoded, Mapping):
                    return dict(decoded)

        raise RuntimeError("meteo_invalid_result")

    def read(self, request: str) -> Mapping[str, Any]:
        tool = "meteo_radar" if _RADAR_RE.search(request) else "meteo_current"

        arguments: dict[str, Any] = {}
        source = ""

        direct = self._direct_location()
        if direct is not None:
            arguments.update(direct)
            source = "telegram_gps"
        else:
            cached = self._cached_location()
            if cached is not None:
                arguments.update({
                    "lat": cached["lat"],
                    "lon": cached["lon"],
                })
                source = "telegram_gps_cache"
            else:
                address = _extract_address(request)
                if address:
                    arguments["address"] = address
                    source = "address"

        if not arguments:
            return {
                "ok": False,
                "status": "LOCATION_REQUIRED",
                "response": (
                    "Mandami la posizione GPS su Telegram oppure scrivi "
                    "l'indirizzo, per esempio: meteo via Padova 100 Milano."
                ),
                "tool": tool,
                "payload": {},
                "read_operations": [],
                "location_source": None,
            }

        try:
            payload = self._call(tool, arguments)
        except (
            OSError,
            RuntimeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            return {
                "ok": False,
                "status": "CONNECTOR_UNAVAILABLE",
                "response": f"Meteo MCP non disponibile: {type(exc).__name__}.",
                "tool": tool,
                "payload": {},
                "read_operations": [],
                "location_source": source,
            }

        ok = bool(payload.get("ok"))

        return {
            "ok": ok,
            "status": str(payload.get("status") or ("OK" if ok else "ERROR")),
            "response": (
                _render_radar(payload)
                if tool == "meteo_radar"
                else _render_current(payload)
            ),
            "tool": tool,
            "payload": payload,
            "read_operations": [tool] if ok else [],
            "location_source": (
                source
                or payload.get("location_source")
            ),
        }


__all__ = ["MeteoMCPReadOnly"]
