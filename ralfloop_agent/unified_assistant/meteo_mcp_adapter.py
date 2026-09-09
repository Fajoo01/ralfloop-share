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


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _precipitation_mm(item: Mapping[str, Any]) -> float:
    values = (
        item.get("precipitation_mm"),
        item.get("precipitation"),
        item.get("rain_mm"),
        item.get("rain"),
        item.get("showers_mm"),
        item.get("showers"),
    )
    return max((_as_float(value) for value in values if value is not None), default=0.0)


def _clock_phrase(value: str) -> str:
    clock = value[11:16] if len(value) >= 16 else value
    if clock == "00:00":
        return "verso mezzanotte"

    try:
        hour = int(clock[:2])
    except (TypeError, ValueError):
        return f"verso le {clock}"

    if hour == 1:
        return "verso l'1"
    return f"verso le {hour}"


def _daypart(value: str) -> str:
    try:
        hour = int(value[11:13])
    except (TypeError, ValueError):
        return "nelle ore successive"

    if 0 <= hour < 6:
        return "durante la notte"
    if hour < 12:
        return "in mattinata"
    if hour < 18:
        return "nel pomeriggio"
    return "in serata"


def _render_current(payload: Mapping[str, Any]) -> str:
    current = dict(payload.get("current") or {})
    current_time = str(current.get("time") or "")
    temperature = current.get("temperature_2m")
    apparent = current.get("apparent_temperature")
    wind = _as_float(current.get("wind_speed_10m"))

    current_rain = max(
        _as_float(current.get("precipitation")),
        _as_float(current.get("rain")),
        _as_float(current.get("showers")),
    )

    location = str(payload.get("location") or "").strip()
    coordinates = bool(
        re.fullmatch(
            r"\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*",
            location,
        )
    )

    if coordinates or not location:
        where = "qui"
    else:
        where = f"a {location}"

    if current_rain >= 10:
        rain_now = "sta piovendo molto forte"
    elif current_rain >= 4:
        rain_now = "sta piovendo forte"
    elif current_rain >= 1:
        rain_now = "sta piovendo moderatamente"
    elif current_rain >= 0.2:
        rain_now = "sta piovendo debolmente"
    else:
        rain_now = "non piove"

    first = f"Adesso {where} {rain_now}"

    if temperature is not None:
        first += f" e ci sono circa {round(_as_float(temperature))} °C"
    first += "."

    lines = [first]

    if (
        temperature is not None
        and apparent is not None
        and abs(_as_float(apparent) - _as_float(temperature)) >= 4
    ):
        lines.append(
            f"La temperatura percepita è di circa {round(_as_float(apparent))} °C."
        )

    future = []
    for item in payload.get("next_hours") or ():
        if not isinstance(item, Mapping):
            continue
        when = str(item.get("time") or "")
        if current_time and when and when <= current_time:
            continue
        future.append(item)

    wet = [
        item
        for item in future
        if _precipitation_mm(item) >= 0.2
    ]

    if current_rain < 0.2:
        if wet:
            first_wet = wet[0]
            when = str(first_wet.get("time") or "")
            lines.append(f"La pioggia è prevista {_clock_phrase(when)}.")
        elif future:
            lines.append(
                "Nelle prossime ore non sono previste piogge significative."
            )

    if wet:
        strongest = max(wet, key=_precipitation_mm)
        strongest_mm = _precipitation_mm(strongest)
        strongest_when = str(strongest.get("time") or "")

        if strongest_mm >= 10:
            lines.append(
                f"Sono previste precipitazioni molto forti "
                f"{_daypart(strongest_when)}."
            )
        elif strongest_mm >= 4:
            lines.append(
                f"Le precipitazioni potrebbero diventare forti "
                f"{_daypart(strongest_when)}."
            )

    if wind >= 30:
        lines.append(f"Vento sostenuto, circa {round(wind)} km/h.")

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
