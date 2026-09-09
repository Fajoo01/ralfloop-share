from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from .meteo_mcp_adapter import MeteoMCPReadOnly


_NAMED_ROUTE_RE = re.compile(
    r"\bda\s+(?P<origin>.+?)\s+"
    r"(?:a|al|alla|all['’]|in)\s+"
    r"(?P<destination>.+?)\s*[?!.]*$",
    re.I,
)

_DESTINATION_PATTERNS = (
    re.compile(
        r"\bcome\s+(?:arrivo|vado|posso\s+andare)\s+"
        r"(?:a|al|alla|all['’]|in)\s+(?P<destination>.+?)\s*[?!.]*$",
        re.I,
    ),
    re.compile(
        r"\bportami\s+(?:a|al|alla|all['’]|in)\s+"
        r"(?P<destination>.+?)\s*[?!.]*$",
        re.I,
    ),
    re.compile(
        r"\bmezzi\s+(?:per|verso)\s+"
        r"(?P<destination>.+?)\s*[?!.]*$",
        re.I,
    ),
    re.compile(
        r"\bpercorso(?:\s+atm|\s+con\s+i\s+mezzi)?\s+"
        r"(?:per|verso|fino\s+a)\s+"
        r"(?P<destination>.+?)\s*[?!.]*$",
        re.I,
    ),
)


def _clean(value: str) -> str:
    return value.strip(" \t\r\n,;:.?!")


def _named_route(text: str) -> tuple[str, str] | None:
    match = _NAMED_ROUTE_RE.search(text)
    if not match:
        return None

    origin = _clean(match.group("origin"))
    destination = _clean(match.group("destination"))

    if len(origin) < 2 or len(destination) < 2:
        return None

    return origin, destination


def _destination(text: str) -> str | None:
    for pattern in _DESTINATION_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue

        value = _clean(match.group("destination"))
        if len(value) >= 2:
            return value

    return None


def _render(payload: Mapping[str, Any]) -> str:
    reply = str(payload.get("reply") or "").strip()
    if reply:
        return reply

    destination = payload.get("destination")
    if isinstance(destination, Mapping):
        label = str(
            destination.get("label")
            or destination.get("name")
            or "destinazione"
        )
    else:
        label = "destinazione"

    lines = list(payload.get("lines") or ())
    duration = payload.get("duration_min")
    walking = payload.get("walking_m")

    out = [f"Percorso ATM per {label}."]

    if lines:
        out.append("Linee: " + ", ".join(str(x) for x in lines) + ".")

    if duration not in (None, ""):
        out.append(f"Tempo stimato: {duration} min.")

    if walking not in (None, ""):
        out.append(f"A piedi: {walking} m.")

    url = str(payload.get("official_route_url") or "")
    if url:
        out.append(f"ATM: {url}")

    return "\n".join(out)


class ATMMCPReadOnly(MeteoMCPReadOnly):
    def __init__(self, context: Mapping[str, Any] | None = None) -> None:
        super().__init__(context)
        self.socket_path = os.getenv(
            "RALF_ATM_MCP_SOCKET",
            "/run/ralf-atm-mcp/mcp.sock",
        )

    def read(self, request: str) -> Mapping[str, Any]:
        explicit = _named_route(request)

        tool: str
        arguments: dict[str, Any]
        source: str

        if explicit is not None:
            origin, destination = explicit
            tool = "atm_route_named"
            arguments = {
                "origin": origin,
                "destination": destination,
            }
            source = "named_origin"
        else:
            destination = _destination(request)

            if not destination:
                return {
                    "ok": False,
                    "status": "DESTINATION_REQUIRED",
                    "response": (
                        "Indicami la destinazione, per esempio: "
                        "portami alla Piscina Suzzani."
                    ),
                    "tool": "atm_route",
                    "payload": {},
                    "read_operations": [],
                    "location_source": None,
                }

            direct = self._direct_location()

            if direct is not None:
                location = direct
                source = "telegram_gps"
            else:
                cached = self._cached_location()
                if cached is not None:
                    location = {
                        "lat": cached["lat"],
                        "lon": cached["lon"],
                    }
                    source = "telegram_gps_cache"
                else:
                    return {
                        "ok": False,
                        "status": "LOCATION_REQUIRED",
                        "response": (
                            "Mandami la posizione GPS su Telegram oppure "
                            "specifica anche la partenza, per esempio: "
                            "come vado da Duomo a Piscina Suzzani?"
                        ),
                        "tool": "atm_route",
                        "payload": {},
                        "read_operations": [],
                        "location_source": None,
                    }

            tool = "atm_route"
            arguments = {
                **location,
                "destination": destination,
            }

        try:
            payload = self._call(tool, arguments)
        except (
            OSError,
            RuntimeError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            return {
                "ok": False,
                "status": "CONNECTOR_UNAVAILABLE",
                "response": f"ATM MCP non disponibile: {type(exc).__name__}.",
                "tool": tool,
                "payload": {},
                "read_operations": [],
                "location_source": source,
            }

        ok = bool(payload.get("ok"))

        return {
            "ok": ok,
            "status": str(payload.get("status") or ("OK" if ok else "ERROR")),
            "response": _render(payload) if ok else "Percorso ATM non disponibile.",
            "tool": tool,
            "payload": payload,
            "read_operations": [tool] if ok else [],
            "location_source": source,
        }


__all__ = ["ATMMCPReadOnly"]
