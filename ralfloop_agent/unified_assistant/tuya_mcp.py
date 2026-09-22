from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Protocol

from .home_provider import HomeAssistantRESTBackend


TUYA_TOOLS = frozenset({
    "tuya_health",
    "tuya_list_devices",
    "tuya_list_entities",
    "tuya_get_state",
    "tuya_call_service",
})

_ALLOWED_SERVICES = frozenset({
    ("light", "turn_on"), ("light", "turn_off"),
    ("switch", "turn_on"), ("switch", "turn_off"),
    ("climate", "turn_on"), ("climate", "turn_off"),
    ("climate", "set_temperature"), ("climate", "set_hvac_mode"),
    ("cover", "open_cover"), ("cover", "close_cover"), ("cover", "stop_cover"),
    ("scene", "turn_on"),
})
_DATA_FIELDS = {
    "turn_on": frozenset(),
    "turn_off": frozenset(),
    "open_cover": frozenset(),
    "close_cover": frozenset(),
    "stop_cover": frozenset(),
    "set_temperature": frozenset({"temperature"}),
    "set_hvac_mode": frozenset({"hvac_mode"}),
}


class TuyaMCPError(RuntimeError):
    pass


class TuyaStateBackend(Protocol):
    def health(self) -> dict[str, Any]: ...
    def list_entities(self) -> tuple[dict[str, Any], ...]: ...
    def read_state(self, entity_id: str) -> dict[str, Any]: ...
    def call_service(self, service: str, entity_id: str, data: dict[str, Any]) -> Any: ...


class TuyaHARegistry:
    def __init__(self, config_dir: str | Path, *, site_map_file: str | Path | None = None) -> None:
        self.config_dir = Path(config_dir)
        self.storage = self.config_dir / ".storage"
        self.site_map_file = Path(site_map_file or os.getenv(
            "TUYA_SITE_MAP_FILE",
            "/home/sibilla-cumana/ralf-memory-rag/current/config/tuya_sites.json",
        ))

    def _device_sites(self) -> dict[str, str]:
        try:
            payload = json.loads(self.site_map_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        sites = payload.get("sites") if isinstance(payload, Mapping) else None
        if not isinstance(sites, Mapping):
            return {}
        result: dict[str, str] = {}
        for site, spec in sites.items():
            site_name = str(site or "").strip().casefold()
            if not site_name or not site_name.replace("_", "").replace("-", "").isalnum():
                continue
            if not isinstance(spec, Mapping):
                continue
            for device_id in spec.get("device_ids") or []:
                value = str(device_id or "").strip()
                if value:
                    result[value] = site_name
        return result

    def _excluded_device_ids(self) -> frozenset[str]:
        try:
            payload = json.loads(self.site_map_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return frozenset()
        sites = payload.get("sites") if isinstance(payload, Mapping) else None
        if not isinstance(sites, Mapping):
            return frozenset()
        result: set[str] = set()
        for spec in sites.values():
            if not isinstance(spec, Mapping):
                continue
            for field in ("retired_devices", "inactive_devices"):
                for row in spec.get(field) or []:
                    if isinstance(row, Mapping) and row.get("device_id"):
                        result.add(str(row["device_id"]).strip())
        return frozenset(result)

    def _load(self, name: str) -> dict[str, Any]:
        path = self.storage / name
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TuyaMCPError(f"tuya_registry_unavailable:{name}") from exc
        if not isinstance(payload, dict):
            raise TuyaMCPError(f"tuya_registry_invalid:{name}")
        return payload

    def config_entry_ids(self) -> frozenset[str]:
        payload = self._load("core.config_entries")
        rows = payload.get("data", {}).get("entries", [])
        ids = {
            str(row.get("entry_id"))
            for row in rows
            if isinstance(row, Mapping) and row.get("domain") == "tuya" and row.get("entry_id")
        }
        if not ids:
            raise TuyaMCPError("tuya_integration_not_configured")
        return frozenset(ids)

    def entities(self, *, include_disabled: bool = False) -> tuple[dict[str, Any], ...]:
        ids = self.config_entry_ids()
        device_sites = self._device_sites()
        excluded_device_ids = self._excluded_device_ids()
        payload = self._load("core.entity_registry")
        rows = payload.get("data", {}).get("entities", [])
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("config_entry_id") not in ids:
                continue
            if row.get("disabled_by") and not include_disabled:
                continue
            entity_id = str(row.get("entity_id") or "")
            device_id = str(row.get("device_id") or "")
            if device_id in excluded_device_ids:
                continue
            if "." not in entity_id:
                continue
            result.append({
                "entity_id": entity_id,
                "domain": entity_id.split(".", 1)[0],
                "device_id": row.get("device_id"),
                "site": device_sites.get(str(row.get("device_id") or ""), "unassigned"),
                "name": row.get("name") or row.get("original_name"),
                "original_name": row.get("original_name"),
                "disabled_by": row.get("disabled_by"),
                "platform": row.get("platform"),
            })
        return tuple(result)

    def devices(self) -> tuple[dict[str, Any], ...]:
        ids = self.config_entry_ids()
        device_sites = self._device_sites()
        excluded_device_ids = self._excluded_device_ids()
        entities = self.entities(include_disabled=True)
        counts: dict[str, int] = {}
        for row in entities:
            device_id = str(row.get("device_id") or "")
            if device_id:
                counts[device_id] = counts.get(device_id, 0) + 1
        payload = self._load("core.device_registry")
        rows = payload.get("data", {}).get("devices", [])
        result: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            config_entries = set(str(x) for x in row.get("config_entries") or [])
            if not config_entries.intersection(ids):
                continue
            device_id = str(row.get("id") or "")
            if device_id in excluded_device_ids:
                continue
            result.append({
                "device_id": device_id,
                "name": row.get("name_by_user") or row.get("name"),
                "site": device_sites.get(device_id, "unassigned"),
                "manufacturer": row.get("manufacturer"),
                "model": row.get("model"),
                "area_id": row.get("area_id"),
                "entity_count": counts.get(device_id, 0),
            })
        return tuple(result)

    def assert_entity(self, entity_id: str) -> dict[str, Any]:
        for row in self.entities(include_disabled=True):
            if row["entity_id"] == entity_id:
                if row.get("disabled_by"):
                    raise TuyaMCPError("tuya_entity_disabled")
                return row
        raise TuyaMCPError("tuya_entity_not_owned")
class TuyaMCPServer:
    def __init__(
        self,
        registry: TuyaHARegistry | None = None,
        backend: TuyaStateBackend | None = None,
    ) -> None:
        config_dir = os.getenv("TUYA_HA_CONFIG_DIR", "/home/sibilla-cumana/homeassistant/config")
        env_file = os.getenv("HA_ENV_FILE", "/home/sibilla-cumana/.secrets/homeassistant.env")
        self.registry = registry or TuyaHARegistry(config_dir)
        self.backend = backend or HomeAssistantRESTBackend.from_environment(env_file=env_file)

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            _tool("tuya_health", "Check Tuya integration and Home Assistant connectivity.", {}, []),
            _tool(
                "tuya_list_devices",
                "List devices belonging to the configured Home Assistant Tuya integration.",
                {"query": _text(200), "limit": _limit(1, 200)},
                [],
            ),
            _tool(
                "tuya_list_entities",
                "List/search entities owned by the Tuya integration, optionally by domain or device.",
                {
                    "query": _text(200),
                    "domain": _text(64),
                    "device_id": _text(128),
                    "include_disabled": {"type": "boolean"},
                    "limit": _limit(1, 300),
                },
                [],
            ),
            _tool(
                "tuya_get_state",
                "Read one Tuya-owned Home Assistant entity state.",
                {"entity_id": _entity_id_schema()},
                ["entity_id"],
            ),
            _tool(
                "tuya_call_service",
                "Call one strictly allowlisted Home Assistant service on a Tuya-owned entity.",
                {
                    "entity_id": _entity_id_schema(),
                    "service": {"type": "string", "enum": sorted({service for _, service in _ALLOWED_SERVICES})},
                    "data": {"type": "object"},
                },
                ["entity_id", "service"],
            ),
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TUYA_TOOLS or not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")
        try:
            if name == "tuya_health":
                return _result(self._health())
            if name == "tuya_list_devices":
                return _result(self._list_devices(arguments))
            if name == "tuya_list_entities":
                return _result(self._list_entities(arguments))
            if name == "tuya_get_state":
                return _result(self._get_state(str(arguments["entity_id"])))
            return _result(self._call_service(arguments))
        except TuyaMCPError as exc:
            return _error(str(exc))
        except (OSError, ValueError, KeyError, TypeError):
            return _error("MALFORMED_REQUEST")
        except Exception:
            return _error("SOURCE_UNAVAILABLE")
    def _health(self) -> dict[str, Any]:
        health = self.backend.health()
        entities = self.registry.entities()
        devices = self.registry.devices()
        live = self.backend.list_entities()
        state_health = _summarize_entity_health(entities, devices, live)
        degraded = bool(state_health["unavailable"] or state_health["missing"])
        return {
            "status": "completed",
            "availability": "degraded" if degraded else "healthy",
            "home_assistant": health,
            "tuya_devices": len(devices),
            "tuya_entities": len(entities),
            "state_health": state_health,
            "writes": 0,
            "sends": 0,
        }

    def _list_devices(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").casefold().strip()
        limit = int(arguments.get("limit", 100))
        rows = list(self.registry.devices())
        if query:
            rows = [row for row in rows if query in json.dumps(row, ensure_ascii=False).casefold()]
        return {
            "status": "completed",
            "items": rows[:limit],
            "count": min(len(rows), limit),
            "total_matches": len(rows),
            "writes": 0,
            "sends": 0,
        }

    def _list_entities(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        query = str(arguments.get("query") or "").casefold().strip()
        domain = str(arguments.get("domain") or "").strip()
        device_id = str(arguments.get("device_id") or "").strip()
        include_disabled = bool(arguments.get("include_disabled", False))
        limit = int(arguments.get("limit", 100))
        rows = list(self.registry.entities(include_disabled=include_disabled))
        if domain:
            rows = [row for row in rows if row.get("domain") == domain]
        if device_id:
            rows = [row for row in rows if str(row.get("device_id") or "") == device_id]
        if query:
            rows = [row for row in rows if query in json.dumps(row, ensure_ascii=False).casefold()]
        live = {row["entity_id"]: row for row in self.backend.list_entities()}
        items: list[dict[str, Any]] = []
        for row in rows[:limit]:
            state = live.get(row["entity_id"])
            items.append({**row, "state": state})
        return {
            "status": "completed",
            "items": items,
            "count": len(items),
            "total_matches": len(rows),
            "writes": 0,
            "sends": 0,
        }

    def _get_state(self, entity_id: str) -> dict[str, Any]:
        metadata = self.registry.assert_entity(entity_id)
        state = self.backend.read_state(entity_id)
        return {
            "status": "completed",
            "entity": metadata,
            "state": state,
            "writes": 0,
            "sends": 0,
        }
    def _call_service(self, arguments: Mapping[str, Any]) -> dict[str, Any]:
        entity_id = str(arguments["entity_id"])
        service = str(arguments["service"])
        data = arguments.get("data") or {}
        if not isinstance(data, Mapping):
            raise TuyaMCPError("tuya_service_data_invalid")
        metadata = self.registry.assert_entity(entity_id)
        domain = str(metadata["domain"])
        if (domain, service) not in _ALLOWED_SERVICES:
            raise TuyaMCPError("tuya_service_not_allowlisted")
        allowed = _DATA_FIELDS.get(service, frozenset())
        if set(data) - allowed:
            raise TuyaMCPError("tuya_service_data_not_allowlisted")
        payload = {key: data[key] for key in allowed if key in data}
        before = self.backend.read_state(entity_id)
        self.backend.call_service(service, entity_id, payload)
        after = self.backend.read_state(entity_id)
        return {
            "status": "completed",
            "entity": metadata,
            "service": service,
            "before": before,
            "after": after,
            "writes": 1,
            "sends": 0,
        }


NON_PHYSICAL_HEALTH_DOMAINS = frozenset({"scene"})


def _summarize_entity_health(
    entities: tuple[dict[str, Any], ...],
    devices: tuple[dict[str, Any], ...],
    live_rows: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    live = {str(row.get("entity_id") or ""): row for row in live_rows if isinstance(row, Mapping)}
    device_names = {
        str(row.get("device_id") or ""): str(row.get("name") or "")
        for row in devices if isinstance(row, Mapping)
    }
    device_sites = {
        str(row.get("device_id") or ""): str(row.get("site") or "unassigned")
        for row in devices if isinstance(row, Mapping)
    }
    site_health: dict[str, dict[str, Any]] = {}
    for row in devices:
        site = str(row.get("site") or "unassigned")
        bucket = site_health.setdefault(site, {"devices": 0, "entities": 0, "available": 0, "unavailable": 0, "unknown": 0, "missing": 0})
        bucket["devices"] += 1
    unavailable: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    ignored_nonphysical: list[dict[str, Any]] = []
    available = 0
    for entity in entities:
        entity_id = str(entity.get("entity_id") or "")
        if not entity_id:
            continue
        domain = str(entity.get("domain") or entity_id.split(".", 1)[0])
        if domain in NON_PHYSICAL_HEALTH_DOMAINS:
            row = live.get(entity_id)
            ignored_nonphysical.append({
                "entity_id": entity_id,
                "domain": domain,
                "site": str(entity.get("site") or "unassigned"),
                "state": str((row or {}).get("state") or "missing").casefold(),
            })
            continue
        device_id = str(entity.get("device_id") or "")
        row = live.get(entity_id)
        site = str(entity.get("site") or device_sites.get(device_id) or "unassigned")
        bucket = site_health.setdefault(site, {"devices": 0, "entities": 0, "available": 0, "unavailable": 0, "unknown": 0, "missing": 0})
        bucket["entities"] += 1
        item = {
            "entity_id": entity_id,
            "device_id": device_id or None,
            "device_name": device_names.get(device_id) or None,
            "site": site,
        }
        if row is None:
            bucket["missing"] += 1
            item["state"] = "missing"
            missing.append(item)
            continue
        state = str(row.get("state") or "").casefold()
        item["state"] = state or "unknown"
        if state == "unavailable":
            unavailable.append(item)
            bucket["unavailable"] += 1
        elif state == "unknown" or not state:
            unknown.append(item)
            bucket["unknown"] += 1
        else:
            available += 1
            bucket["available"] += 1
    degraded_devices: dict[str, dict[str, Any]] = {}
    for item in unavailable + missing:
        device_id = str(item.get("device_id") or "")
        key = device_id or f"entity:{item['entity_id']}"
        row = degraded_devices.setdefault(key, {
            "device_id": device_id or None,
            "device_name": item.get("device_name"),
            "site": item.get("site") or "unassigned",
            "entity_count": 0,
            "unavailable": 0,
            "missing": 0,
            "entities": [],
        })
        row["entity_count"] += 1
        row[str(item["state"])] += 1
        row["entities"].append(item["entity_id"])
    grouped = sorted(
        degraded_devices.values(),
        key=lambda row: (-int(row["entity_count"]), str(row.get("device_name") or "")),
    )
    for bucket in site_health.values():
        if int(bucket["entities"]) == 0:
            bucket["status"] = "no_entities"
        elif int(bucket["available"]) == 0 and int(bucket["unavailable"]) > 0:
            bucket["status"] = "offline"
        elif int(bucket["unavailable"]) > 0 or int(bucket["missing"]) > 0:
            bucket["status"] = "degraded"
        elif int(bucket["available"]) > 0:
            bucket["status"] = "online"
        else:
            bucket["status"] = "unknown"
    return {
        "available": available,
        "unavailable": len(unavailable),
        "unknown": len(unknown),
        "missing": len(missing),
        "unavailable_entities": unavailable[:50],
        "unknown_entities": unknown[:50],
        "missing_entities": missing[:50],
        "ignored_nonphysical": len(ignored_nonphysical),
        "ignored_nonphysical_entities": ignored_nonphysical[:100],
        "degraded_devices": grouped[:50],
        "site_health": dict(sorted(site_health.items())),
    }


def _entity_id_schema() -> dict[str, Any]:
    return {"type": "string", "pattern": r"^[a-z_]+\.[a-z0-9_]+$", "maxLength": 180}
def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _text(maximum: int) -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": maximum}


def _limit(minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "minimum": minimum, "maximum": maximum}


def _result(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = {"ok": True, **dict(payload)}
    return {
        "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
        "structuredContent": data,
        "isError": False,
    }
def _error(code: str) -> dict[str, Any]:
    data = {"ok": False, "status": code, "writes": 0, "sends": 0}
    return {
        "content": [{"type": "text", "text": code}],
        "structuredContent": data,
        "isError": True,
    }


__all__ = [
    "TUYA_TOOLS",
    "TuyaHARegistry",
    "TuyaMCPError",
    "TuyaMCPServer",
]
