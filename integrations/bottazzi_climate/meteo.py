from __future__ import annotations

from dataclasses import dataclass
import json
import socket
from typing import Any, Mapping


@dataclass(frozen=True)
class OutdoorWeatherContext:
    recent_mean_c: float | None
    history_hours: int
    status: str
    current_c: float | None = None


def parse_meteo_context(
    payload: Mapping[str, Any], *, min_recent_hours: int = 72
) -> OutdoorWeatherContext:
    body: Mapping[str, Any] = payload
    structured = payload.get("structuredContent")
    if isinstance(structured, Mapping):
        body = structured

    try:
        hours = int(body.get("recent_temperature_hours") or 0)
    except (TypeError, ValueError):
        hours = 0
    value = body.get("recent_temperature_mean_c")
    current = body.get("current") if isinstance(body.get("current"), Mapping) else {}
    current_value = current.get("temperature_2m")
    try:
        current_c = float(current_value) if current_value is not None else None
    except (TypeError, ValueError):
        current_c = None
    if value is None or hours < max(int(min_recent_hours), 1):
        return OutdoorWeatherContext(None, hours, "insufficient_history", current_c)
    try:
        return OutdoorWeatherContext(float(value), hours, "ok", current_c)
    except (TypeError, ValueError):
        return OutdoorWeatherContext(None, hours, "invalid_temperature", current_c)


def _rpc(
    sock_path: str,
    address: str,
    timeout_s: float,
    lat: float | None = None,
    lon: float | None = None,
) -> Mapping[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(float(timeout_s))
    try:
        sock.connect(sock_path)
        stream = sock.makefile("rwb", buffering=0)

        def request(value: Mapping[str, Any]) -> Mapping[str, Any]:
            stream.write((json.dumps(value, separators=(",", ":")) + "\n").encode())
            raw = stream.readline()
            if not raw:
                raise RuntimeError("meteo_mcp_eof")
            response = json.loads(raw)
            if not isinstance(response, Mapping):
                raise RuntimeError("meteo_mcp_invalid_response")
            return response

        request({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "bottazzi-climate", "version": "1"},
            },
        })
        stream.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        arguments: dict[str, Any]
        if lat is not None and lon is not None:
            arguments = {"lat": float(lat), "lon": float(lon)}
        elif address:
            arguments = {"address": address}
        else:
            raise RuntimeError("meteo_location_missing")
        response = request({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "meteo_current", "arguments": arguments},
        })
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise RuntimeError("meteo_mcp_missing_result")
        return result
    finally:
        sock.close()


def fetch_outdoor_weather(policy: Mapping[str, Any]) -> OutdoorWeatherContext:
    cfg = policy.get("weather")
    if not isinstance(cfg, Mapping) or not cfg.get("enabled", False):
        return OutdoorWeatherContext(None, 0, "disabled")
    try:
        payload = _rpc(
            str(cfg.get("socket_path") or "/run/ralf-meteo-mcp/mcp.sock"),
            str(cfg.get("address") or "").strip(),
            float(cfg.get("timeout_seconds", 5.0)),
            float(cfg["lat"]) if cfg.get("lat") is not None else None,
            float(cfg["lon"]) if cfg.get("lon") is not None else None,
        )
        return parse_meteo_context(
            payload, min_recent_hours=int(cfg.get("min_recent_hours", 72))
        )
    except Exception:
        return OutdoorWeatherContext(None, 0, "unavailable")
