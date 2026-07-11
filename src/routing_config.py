from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROUTING_CONFIG = PROJECT_ROOT / "config" / "capability_routing.json"


@lru_cache(maxsize=4)
def load_routing_config(path: str | None = None) -> dict[str, Any]:
    config_path = Path(path or os.getenv("RALF_ROUTING_CONFIG") or DEFAULT_ROUTING_CONFIG)
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def trigger_matches(trigger: str, goal: str) -> bool:
    trigger = trigger.strip().lower()
    if not trigger or trigger not in goal:
        return False
    if len(trigger) <= 3 and trigger.isalnum():
        pattern = rf"(?<![a-z0-9_]){re.escape(trigger)}(?![a-z0-9_])"
        return re.search(pattern, goal) is not None
    return True


def any_trigger_matches(triggers: list[str] | tuple[str, ...], goal: str) -> bool:
    return any(trigger_matches(str(trigger), goal) for trigger in triggers)


def configured_jury_roles(config: dict[str, Any]) -> list[str]:
    jury = dict(config.get("jury") or {})
    mode_file = jury.get("mode_file")
    if mode_file:
        try:
            payload = json.loads(Path(mode_file).read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        roles = [str(role.get("id")) for role in payload.get("roles", []) if role.get("id")]
        judge = ((payload.get("judge") or {}).get("id"))
        reviewer = ((payload.get("final_reviewer") or {}).get("id"))
        roles.extend(str(value) for value in (judge, reviewer) if value)
        if roles:
            return _dedupe(roles)
    return _dedupe(str(role) for role in jury.get("fallback_roles", []) if role)


def mcp_keywords(config: dict[str, Any]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    entries = []
    for item in config.get("mcp_keywords", []) or []:
        name = str(item.get("name") or "").strip()
        triggers = tuple(str(value).lower() for value in item.get("triggers", []) if str(value).strip())
        if name and triggers:
            entries.append((name, triggers))
    return tuple(entries)


def local_jury_style(config: dict[str, Any], trigger: str | None = None) -> str:
    jury = dict(config.get("jury") or {})
    style_by_trigger = dict(jury.get("style_by_trigger") or {})
    return str(style_by_trigger.get(trigger or "") or jury.get("default_style") or "sequential")


def local_jury_style_source(config: dict[str, Any]) -> str:
    jury = dict(config.get("jury") or {})
    return str(jury.get("style_selection_source") or "ralfloop_local_policy")


def collaboration_backend_config(config: dict[str, Any], name: str) -> dict[str, Any]:
    backends = dict(config.get("collaboration_backends") or {})
    return dict(backends.get(name) or {})


def verification_config(config: dict[str, Any], mode: str) -> dict[str, Any]:
    verification = dict(config.get("verification") or {})
    selected = dict(verification.get(mode) or verification.get("default") or {})
    return {
        "enabled": bool(selected.get("enabled", True)),
        "verifier_type": str(selected.get("verifier_type") or "deterministic"),
        "criteria": list(selected.get("criteria") or []),
        "blocking": bool(selected.get("blocking", True)),
        "judge_provider": selected.get("judge_provider"),
    }


def _dedupe(values) -> list[str]:
    seen = set()
    out = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
