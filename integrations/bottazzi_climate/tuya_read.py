from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, UnixMCPTransport

from .thermostat import ThermostatReading, normalize_ha_thermostat_state


class ClimateTuyaReadError(RuntimeError):
    pass


@dataclass(frozen=True)
class IndoorClimateContext:
    reading: ThermostatReading
    status: str
    entity_id: str
    site: str
    writes: int
    sends: int
    raw_state: Mapping[str, Any]


def _structured(result: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = result.get("structuredContent")
    if not isinstance(payload, Mapping):
        raise ClimateTuyaReadError("tuya_structured_content_missing")
    if payload.get("ok") is False:
        raise ClimateTuyaReadError(str(payload.get("status") or "tuya_read_failed"))
    return payload


def fetch_sede_indoor_temperature(policy: Mapping[str, Any]) -> IndoorClimateContext:
    cfg = policy["boiler_thermostat"]
    tuya_cfg = policy.get("tuya_read") or {}
    socket_path = str(tuya_cfg.get("socket_path") or "/run/ralf-tuya-mcp/mcp.sock")
    timeout = float(tuya_cfg.get("timeout_seconds", 20.0))
    entity_id = str(cfg["entity_id"])
    session = MCPClientSession(
        UnixMCPTransport(socket_path), timeout=timeout,
        client_name="bottazzi-sede-climate-tuya-read",
    )
    try:
        session.initialize()
        tools = {tool.name for tool in session.list_tools()}
        if "tuya_get_state" not in tools:
            raise ClimateTuyaReadError("tuya_get_state_not_discovered")
        payload = _structured(session.call_tool(
            "tuya_get_state", {"entity_id": entity_id}
        ))
    finally:
        session.close()
    writes = int(payload.get("writes") or 0)
    sends = int(payload.get("sends") or 0)
    if writes or sends:
        raise ClimateTuyaReadError("unexpected_tuya_side_effect")
    entity = payload.get("entity")
    state = payload.get("state")
    if not isinstance(entity, Mapping) or not isinstance(state, Mapping):
        raise ClimateTuyaReadError("tuya_state_shape_invalid")
    observed_entity = str(entity.get("entity_id") or "")
    site = str(entity.get("site") or "")
    if observed_entity != entity_id:
        raise ClimateTuyaReadError("tuya_entity_mismatch")
    if site.casefold() != "sede":
        raise ClimateTuyaReadError("tuya_entity_wrong_site")
    reading = normalize_ha_thermostat_state(state, policy)
    status = "ok" if reading.current_temperature_c is not None else "temperature_unavailable"
    return IndoorClimateContext(
        reading=reading,
        status=status,
        entity_id=entity_id,
        site=site,
        writes=writes,
        sends=sends,
        raw_state=dict(state),
    )
