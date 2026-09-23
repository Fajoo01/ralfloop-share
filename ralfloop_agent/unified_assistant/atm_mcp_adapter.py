from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping

from .meteo_mcp_adapter import MeteoMCPReadOnly


_NAMED_ROUTE_RE = re.compile(
    r"\bda\s+(?P<origin>.+?)\s+"
    r"(?:a|ad|al|alla|all['’]|in)\s+"
    r"(?P<destination>.+?)\s*[?!.]*$",
    re.I,
)


_TRAILING_TRANSPORT_QUALIFIER_RE = re.compile(
    r"\s+con\s+(?:atm|(?:i\s+)?mezzi(?:\s+atm)?)\s*$",
    re.I,
)

_DESTINATION_PATTERNS = (
    re.compile(
        r"\b(?:devo|voglio|vorrei)\s+(?:andare|arrivare)\s+"
        r"(?:da|dal|dalla|dallo|dai|dagli|dalle|a|ad|al|alla|allo|ai|agli|alle|all['’]|in)\s+"
        r"(?P<destination>.+?)\s*[?!.]*$",
        re.I,
    ),
    re.compile(
        r"\bcome\s+(?:arrivo|vado|posso\s+andare)\s+"
        r"(?:a|ad|al|alla|all['’]|in)\s+(?P<destination>.+?)\s*[?!.]*$",
        re.I,
    ),
    re.compile(
        r"\bportami\s+(?:a|ad|al|alla|all['’]|in)\s+"
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
    destination = _clean(
        _TRAILING_TRANSPORT_QUALIFIER_RE.sub("", destination)
    )

    if len(origin) < 2 or len(destination) < 2:
        return None

    return origin, destination


def _destination(text: str) -> str | None:
    # With imperative "portami", a lone "da X" names the destination;
    # "da X a Y" remains the explicit origin/destination form above.
    single = re.fullmatch(
        r"\s*portami\s+(?:da|dal|dalla|dallo|dai|dagli|dalle)\s+(.+?)\s*[?!.]*\s*",
        text, re.I,
    )
    if single:
        value = _clean(single.group(1))
        if not re.search(r"\s+(?:a|ad|al|alla|all['’]|in)\s+", value, re.I):
            value = re.sub(r"^(?:dal|dalla|dallo|dai|dagli|dalle)\s+", "", value, flags=re.I)
            return value if len(value) >= 2 else None

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
        self.socket_timeout_s = 4.0
        self.socket_path = os.getenv(
            "RALF_ATM_MCP_SOCKET",
            "/run/ralf-atm-mcp/mcp.sock",
        )

    def read(self, request: str) -> Mapping[str, Any]:
        explicit = _named_route(request)
        source = "named_origin" if explicit else "telegram_gps"
        if explicit is None:
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
                    app_surface = str(self.context.get("assistant_surface") or "") == "assistant_v1"
                    return {
                        "ok": False,
                        "status": "LOCATION_REQUIRED",
                        "response": (
                            "Mi serve il punto di partenza. Posso usare la posizione del telefono; "
                            "se preferisci, scrivimi la partenza, per esempio: "
                            "come vado da Duomo a Piscina Suzzani?"
                            if app_surface else
                            "Mandami la posizione GPS su Telegram oppure specifica anche la partenza, "
                            "per esempio: come vado da Duomo a Piscina Suzzani?"
                        ),
                        "tool": "atm_route",
                        "payload": {},
                        "read_operations": [],
                        "location_source": None,
                    }

        try:
            from openshell_backend import atm_telegram

            class _Realtime:
                def __init__(inner) -> None:
                    inner._cache: dict[tuple[str, tuple[str, ...] | None], Mapping[str, Any]] = {}

                def snapshot(
                    inner,
                    stop_code: str,
                    lines: list[str] | None = None,
                ) -> Mapping[str, Any]:
                    key = (
                        str(stop_code),
                        tuple(lines) if lines is not None else None,
                    )

                    cached = inner._cache.get(key)
                    if cached is not None:
                        return cached

                    arguments: dict[str, Any] = {
                        "stop_code": str(stop_code),
                    }

                    if lines is not None:
                        arguments["lines"] = list(lines)

                    try:
                        response = self._call(
                            "atm_realtime_waits",
                            arguments,
                        )
                    except Exception:
                        response = {
                            "ok": False,
                            "status": "not_available",
                            "stop_code": str(stop_code),
                            "arrivals": {},
                            "observations": [],
                            "source": "atm_mcp_unavailable",
                        }

                    inner._cache[key] = response
                    return response

                def batch(
                    inner,
                    queries: list[Mapping[str, Any]],
                ) -> Mapping[str, Mapping[str, Any]]:
                    normalized = []
                    for query in queries:
                        stop_code = str(query.get("stop_code") or "").strip()
                        if not stop_code:
                            continue
                        raw_lines = query.get("lines")
                        lines = (
                            [str(item) for item in raw_lines]
                            if isinstance(raw_lines, list)
                            else None
                        )
                        normalized.append({
                            "stop_code": stop_code,
                            "lines": lines,
                        })

                    if not normalized:
                        return {}

                    try:
                        response = self._call(
                            "atm_realtime_batch",
                            {"queries": normalized},
                        )
                    except Exception:
                        return {}

                    output: dict[str, Mapping[str, Any]] = {}
                    for item in response.get("results") or []:
                        if not isinstance(item, Mapping):
                            continue
                        stop_code = str(item.get("stop_code") or "").strip()
                        if not stop_code:
                            continue
                        output[stop_code] = dict(item)
                        lines = next(
                            (
                                query.get("lines")
                                for query in normalized
                                if query.get("stop_code") == stop_code
                            ),
                            None,
                        )
                        key = (
                            stop_code,
                            tuple(lines) if lines is not None else None,
                        )
                        inner._cache[key] = dict(item)
                    return output

                def waits(
                    inner,
                    stop_code: str,
                    lines: list[str],
                ) -> Mapping[str, str]:
                    response = inner.snapshot(stop_code, lines)

                    if not response.get("ok"):
                        return {}

                    arrivals = response.get("arrivals") or {}

                    if not isinstance(arrivals, Mapping):
                        return {}

                    return {
                        str(line): str(wait)
                        for line, wait in arrivals.items()
                    }

            if explicit is not None:
                payload = atm_telegram.build_named_plan(
                    explicit[0],
                    explicit[1],
                    _Realtime(),
                )
            else:
                payload = atm_telegram.build_plan(
                    location["lat"],
                    location["lon"],
                    destination,
                    _Realtime(),
                )

            payload = dict(payload)

            route_mode = str(payload.get("route_mode") or "").strip()
            plan_ok = bool(route_mode) and route_mode not in {
                "official_atm_lookup_failed",
            }

            if plan_ok:
                payload["reply"] = atm_telegram.render_reply(payload)

            payload["ok"] = plan_ok

            # Il planner usa datetime internamente per ranking/ETA.
            # L'envelope Unified deve invece essere JSON serializzabile.
            payload = json.loads(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    default=lambda value: (
                        value.isoformat()
                        if hasattr(value, "isoformat")
                        else str(value)
                    ),
                )
            )

            tool = "atm_realtime_waits"
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
                "tool": "atm_realtime_waits",
                "payload": {},
                "read_operations": [],
                "location_source": source,
            }

        ok = bool(payload.get("ok"))

        return {
            "ok": ok,
            "status": str(payload.get("status") or ("OK" if ok else "ERROR")),
            "response": _render(payload) if ok else "Percorso ATM non disponibile.",
            "tool": "atm_realtime_waits",
            "payload": payload,
            "read_operations": ["atm_realtime_waits"] if ok else [],
            "location_source": source,
        }


__all__ = ["ATMMCPReadOnly"]
