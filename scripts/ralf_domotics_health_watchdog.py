#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, UnixMCPTransport
from ralfloop_agent.unified_assistant.home_provider import HomeAssistantRESTBackend
from ralfloop_agent.unified_assistant.tuya_mcp import TuyaHARegistry


def load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def write_state(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def fetch_health(socket_path: str) -> dict[str, Any]:
    with MCPClientSession(
        UnixMCPTransport(socket_path, connect_timeout=1.0),
        timeout=8.0,
        client_name="ralf-domotics-watchdog",
    ) as client:
        names = {tool.name for tool in client.list_tools()}
        if "tuya_health" not in names:
            raise RuntimeError("tuya_health_not_discovered")
        result = client.call_tool("tuya_health", {})
    payload = result.get("structuredContent")
    if not isinstance(payload, Mapping) or payload.get("ok") is False:
        raise RuntimeError("tuya_health_malformed")
    return dict(payload)


def entity_ids(rows: Any) -> set[str]:
    if not isinstance(rows, list):
        return set()
    return {str(row.get("entity_id")) for row in rows if isinstance(row, Mapping) and row.get("entity_id")}


def degraded_device_keys(rows: Any) -> set[str]:
    return set(degraded_device_sites(rows))


def degraded_device_sites(rows: Any) -> dict[str, str]:
    if not isinstance(rows, list):
        return {}
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        device_id = str(row.get("device_id") or "").strip()
        entities = row.get("entities") or []
        key = "device:" + device_id if device_id else ("entity:" + str(entities[0]) if entities else "")
        if key:
            result[key] = str(row.get("site") or "unassigned")
    return result


def recovery_eligible_devices(rows: Any, site_health: Mapping[str, Any]) -> tuple[dict[str, str], list[str]]:
    sites = degraded_device_sites(rows)
    eligible: dict[str, str] = {}
    suppressed: list[str] = []
    for key, site in sites.items():
        summary = site_health.get(site) if isinstance(site_health, Mapping) else None
        status = str(summary.get("status") or "") if isinstance(summary, Mapping) else ""
        if status == "offline":
            suppressed.append(key)
        else:
            eligible[key] = site
    return eligible, sorted(suppressed)


def previous_degraded_sets(previous: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    stored = set(previous.get("degraded_device_keys") or [])
    if "recovery_eligible_device_keys" in previous:
        return stored, set(previous.get("recovery_eligible_device_keys") or [])
    # Migration from the first site-aware state format, where degraded_device_keys
    # accidentally contained only recovery-eligible devices.
    suppressed = set(previous.get("recovery_suppressed_site_offline") or [])
    return stored | suppressed, stored


def next_reload_candidates(
    current_eligible: set[str],
    previous_eligible: set[str],
    prior_candidates: Mapping[str, Any],
    *,
    baseline: bool,
) -> dict[str, int]:
    if baseline:
        return {}
    candidates: dict[str, int] = {}
    for key in current_eligible:
        if key not in previous_eligible:
            candidates[key] = 1
        elif key in prior_candidates:
            candidates[key] = int(prior_candidates[key]) + 1
    return candidates


def reload_tuya(config_dir: str, env_file: str) -> dict[str, Any]:
    registry = TuyaHARegistry(config_dir)
    backend = HomeAssistantRESTBackend.from_environment(env_file=env_file, timeout=20.0)
    entry_ids = sorted(registry.config_entry_ids())
    results = []
    for entry_id in entry_ids:
        try:
            backend.reload_config_entry(entry_id)
            results.append({"entry_id": entry_id, "ok": True})
        except Exception as exc:
            results.append({"entry_id": entry_id, "ok": False, "error": type(exc).__name__})
    return {"attempted": len(entry_ids), "results": results, "ok": bool(results) and all(x["ok"] for x in results)}


def append_transition_events(path: Path, event: Mapping[str, Any], entity_sites: Mapping[str, str]) -> int:
    kinds = (
        ("new_unavailable", "DOMOTICS_ENTITY_UNAVAILABLE"),
        ("recovered", "DOMOTICS_ENTITY_RECOVERED"),
        ("new_missing", "DOMOTICS_ENTITY_MISSING"),
        ("returned_missing", "DOMOTICS_ENTITY_RETURNED"),
    )
    rows: list[dict[str, Any]] = []
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    for field, event_type in kinds:
        for entity_id in event.get(field) or []:
            identity = f"{event_type}|{entity_id}|{ts}"
            rows.append({
                "event_id": "domotics_" + hashlib.sha256(identity.encode()).hexdigest()[:20],
                "ts": ts,
                "source": "domotics_watchdog",
                "event": event_type,
                "entity_id": entity_id,
                "site": str(entity_sites.get(str(entity_id)) or "unassigned"),
            })
    if not rows:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", default="/run/ralf-tuya-mcp/mcp.sock")
    parser.add_argument("--state", default="/var/lib/ralf-domotics-watchdog/state.json")
    parser.add_argument("--events", default="/var/lib/ralf-domotics-watchdog/events.jsonl")
    parser.add_argument("--ha-config-dir", default="/home/sibilla-cumana/homeassistant/config")
    parser.add_argument("--ha-env-file", default="/home/sibilla-cumana/.secrets/homeassistant.env")
    parser.add_argument("--persistence", type=int, default=2)
    parser.add_argument("--reload-cooldown", type=float, default=1800.0)
    parser.add_argument("--post-reload-wait", type=float, default=8.0)
    parser.add_argument("--no-reload", action="store_true")
    args = parser.parse_args()

    state_path = Path(args.state)
    previous = load_state(state_path)
    health = fetch_health(args.socket)
    state_health = health.get("state_health") or {}
    unavailable_rows = state_health.get("unavailable_entities") or []
    missing_rows = state_health.get("missing_entities") or []
    current_unavailable = entity_ids(unavailable_rows)
    current_missing = entity_ids(missing_rows)
    current_entity_sites = {
        str(row.get("entity_id")): str(row.get("site") or "unassigned")
        for row in list(unavailable_rows) + list(missing_rows)
        if isinstance(row, Mapping) and row.get("entity_id")
    }
    previous_entity_sites = {str(k): str(v) for k, v in (previous.get("entity_sites") or {}).items()}
    transition_entity_sites = {**previous_entity_sites, **current_entity_sites}
    site_health = state_health.get("site_health") or {}
    all_degraded_device_sites = degraded_device_sites(state_health.get("degraded_devices"))
    current_degraded_devices = set(all_degraded_device_sites)
    eligible_device_sites, suppressed_offline_devices = recovery_eligible_devices(
        state_health.get("degraded_devices"), site_health
    )
    current_recovery_eligible = set(eligible_device_sites)
    previous_unavailable = set(previous.get("unavailable_entities") or [])
    previous_missing = set(previous.get("missing_entities") or [])
    previous_degraded_devices, previous_recovery_eligible = previous_degraded_sets(previous)
    baseline = not bool(previous.get("initialized"))

    prior_candidates = previous.get("reload_candidates") or {}
    candidates = next_reload_candidates(
        current_recovery_eligible, previous_recovery_eligible, prior_candidates, baseline=baseline
    )

    now = time.time()
    last_reload = float(previous.get("last_reload_epoch") or 0.0)
    persistent = sorted(k for k, count in candidates.items() if count >= max(1, args.persistence))
    reload_due = bool(persistent) and now - last_reload >= max(0.0, args.reload_cooldown) and not args.no_reload

    event = {
        "initialized": True,
        "status": "baseline_initialized" if baseline else "steady",
        "availability": health.get("availability"),
        "counts": {key: int(state_health.get(key) or 0) for key in ("available", "unavailable", "unknown", "missing")},
        "new_unavailable": [] if baseline else sorted(current_unavailable - previous_unavailable),
        "recovered": [] if baseline else sorted(previous_unavailable - current_unavailable),
        "new_missing": [] if baseline else sorted(current_missing - previous_missing),
        "returned_missing": [] if baseline else sorted(previous_missing - current_missing),
        "new_degraded_devices": [] if baseline else sorted(current_degraded_devices - previous_degraded_devices),
        "recovered_devices": [] if baseline else sorted(previous_degraded_devices - current_degraded_devices),
        "unavailable_entities": sorted(current_unavailable),
        "missing_entities": sorted(current_missing),
        "degraded_device_keys": sorted(current_degraded_devices),
        "recovery_eligible_device_keys": sorted(current_recovery_eligible),
        "degraded_devices": state_health.get("degraded_devices") or [],
        "site_health": site_health,
        "offline_sites": sorted(
            site for site, row in site_health.items()
            if isinstance(row, Mapping) and row.get("status") == "offline"
        ),
        "recovery_suppressed_site_offline": suppressed_offline_devices,
        "entity_sites": current_entity_sites,
        "reload_candidates": candidates,
        "persistent_reload_candidates": persistent,
        "last_reload_epoch": last_reload,
        "reload_due": reload_due,
    }
    if not baseline and any(event[key] for key in ("new_unavailable", "recovered", "new_missing", "returned_missing", "new_degraded_devices", "recovered_devices")):
        event["status"] = "changed"

    if reload_due:
        event["reload"] = reload_tuya(args.ha_config_dir, args.ha_env_file)
        event["last_reload_epoch"] = now
        event["status"] = "reload_attempted"
        if event["reload"].get("ok"):
            time.sleep(max(0.0, min(args.post_reload_wait, 30.0)))
            event["post_reload_health"] = fetch_health(args.socket).get("state_health") or {}
            event["reload_candidates"] = {}

    event["transition_events_appended"] = 0 if baseline else append_transition_events(Path(args.events), event, transition_entity_sites)
    write_state(state_path, event)
    print(json.dumps(event, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
