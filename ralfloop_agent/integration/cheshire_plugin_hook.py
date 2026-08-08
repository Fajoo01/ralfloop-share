from __future__ import annotations

import os
import sys
from typing import Any


async def try_domain_orchestrator(message: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    return try_domain_orchestrator_sync(message, context)


def try_domain_orchestrator_sync(message: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    if os.getenv("RALFLOOP_ENABLE_DOMAIN_ORCHESTRATOR", "0") != "1":
        return {"handled": False, "legacy_fallback_required": True, "fallback_reason": "domain_orchestrator_disabled"}
    bridge_path = os.getenv("RALFLOOP_DOMAIN_BRIDGE_PATH", "")
    if bridge_path:
        allowed = os.getenv("RALFLOOP_DOMAIN_RUNTIME_PATH", "/opt/ralfloop-domain-runtime")
        if bridge_path != allowed:
            return {"handled": False, "legacy_fallback_required": True, "fallback_reason": "invalid_domain_bridge_path"}
        if bridge_path not in sys.path:
            sys.path.insert(0, bridge_path)
    from .cheshire_domain_bridge import CheshireDomainBridge, CheshireDomainBridgeRequest

    text = _extract_text(message)
    req = CheshireDomainBridgeRequest.from_message(text, context or {})
    bridge = CheshireDomainBridge.from_env()
    result = bridge.execute(req)
    payload = bridge.to_cheshire_response(result)
    payload["bridge_status"] = result.status
    payload["request_id"] = req.request_id
    return payload


def _extract_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return str(message.get("text") or message.get("content") or "")
    return str(getattr(message, "text", None) or getattr(message, "content", None) or "")
