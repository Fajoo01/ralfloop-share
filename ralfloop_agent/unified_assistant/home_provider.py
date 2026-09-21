from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_SERVICE_ALLOWLIST = frozenset({
    ("light", "turn_on"), ("light", "turn_off"),
    ("switch", "turn_on"), ("switch", "turn_off"),
    ("climate", "turn_on"), ("climate", "turn_off"),
    ("climate", "set_temperature"), ("climate", "set_hvac_mode"),
    ("cover", "open_cover"), ("cover", "close_cover"), ("cover", "stop_cover"),
    ("scene", "turn_on"),
})
_DATA_FIELDS = {
    "turn_on": frozenset(), "turn_off": frozenset(), "open_cover": frozenset(),
    "close_cover": frozenset(), "stop_cover": frozenset(),
    "set_temperature": frozenset({"temperature"}),
    "set_hvac_mode": frozenset({"hvac_mode"}),
}


class HomeAssistantProviderError(RuntimeError):
    pass


class HomeAssistantRESTBackend:
    """Least-privilege HA REST adapter. Token never leaves request headers."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 20.0,
        requester: Callable[[str, str, dict[str, Any] | None], Any] | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")) or not token:
            raise HomeAssistantProviderError("home_provider_not_configured")
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = max(1.0, min(float(timeout), 60.0))
        self._requester = requester

    @classmethod
    def from_environment(
        cls,
        *,
        env_file: str | Path | None = None,
        timeout: float = 20.0,
    ) -> "HomeAssistantRESTBackend":
        values = dict(os.environ)
        path = Path(env_file or os.path.expanduser("~/.secrets/homeassistant.env"))
        if path.exists():
            values.update({key: value for key, value in _read_env(path).items() if key not in values})
        return cls(values.get("HA_URL", ""), values.get("HA_TOKEN", ""), timeout=timeout)

    def health(self) -> dict[str, Any]:
        value = self._request("GET", "/api/", None)
        return {"ok": isinstance(value, dict), "base_url": self.base_url}

    def list_entities(self) -> tuple[dict[str, Any], ...]:
        value = self._request("GET", "/api/states", None)
        if not isinstance(value, list):
            raise HomeAssistantProviderError("home_states_invalid")
        return tuple(_state(row) for row in value if isinstance(row, dict))

    def list_services(self) -> tuple[dict[str, Any], ...]:
        value = self._request("GET", "/api/services", None)
        if not isinstance(value, list):
            raise HomeAssistantProviderError("home_services_invalid")
        return tuple(dict(row) for row in value if isinstance(row, dict))

    def read_state(self, entity_id: str) -> dict[str, Any]:
        _validate_entity_id(entity_id)
        value = self._request("GET", f"/api/states/{entity_id}", None)
        if not isinstance(value, dict) or value.get("entity_id") != entity_id:
            raise HomeAssistantProviderError("home_state_invalid")
        return _state(value)

    def call_service(self, service: str, entity_id: str, data: dict[str, Any]) -> Any:
        domain = _validate_entity_id(entity_id)
        if (domain, service) not in _SERVICE_ALLOWLIST:
            raise HomeAssistantProviderError("home_service_not_allowlisted")
        allowed_fields = _DATA_FIELDS.get(service, frozenset())
        if set(data) - allowed_fields:
            raise HomeAssistantProviderError("home_service_data_not_allowlisted")
        payload = {key: data[key] for key in allowed_fields if key in data}
        payload["entity_id"] = entity_id
        return self._request("POST", f"/api/services/{domain}/{service}", payload)

    def reload_config_entry(self, entry_id: str) -> Any:
        value = str(entry_id or "").strip()
        if not value or len(value) > 128 or not all(ch.isalnum() or ch in "-_" for ch in value):
            raise HomeAssistantProviderError("home_config_entry_invalid")
        return self._request(
            "POST",
            "/api/services/homeassistant/reload_config_entry",
            {"entry_id": value},
        )

    def _request(self, method: str, path: str, payload: dict[str, Any] | None) -> Any:
        if self._requester is not None:
            return self._requester(method, path, payload)
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            self.base_url + path,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raise HomeAssistantProviderError(f"home_http_{exc.code}") from exc
        except (OSError, URLError) as exc:
            raise HomeAssistantProviderError("home_provider_unavailable") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise HomeAssistantProviderError("home_response_invalid") from exc


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in {"HA_URL", "HA_TOKEN"}:
            values[key] = value.strip().strip("'\"")
    return values


def _validate_entity_id(entity_id: str) -> str:
    if entity_id == "all" or "." not in entity_id:
        raise HomeAssistantProviderError("home_entity_invalid")
    domain, name = entity_id.split(".", 1)
    if not domain.replace("_", "").isalnum() or not name.replace("_", "").isalnum():
        raise HomeAssistantProviderError("home_entity_invalid")
    return domain


def _state(value: dict[str, Any]) -> dict[str, Any]:
    attributes = value.get("attributes") if isinstance(value.get("attributes"), dict) else {}
    return {
        "entity_id": str(value.get("entity_id") or ""),
        "state": value.get("state"),
        "friendly_name": attributes.get("friendly_name"),
        "temperature": attributes.get("temperature"),
        "current_temperature": attributes.get("current_temperature"),
        "unit_of_measurement": attributes.get("unit_of_measurement"),
        "device_class": attributes.get("device_class"),
        "supported_features": attributes.get("supported_features"),
        "last_changed": value.get("last_changed"),
    }


__all__ = ["HomeAssistantProviderError", "HomeAssistantRESTBackend"]
