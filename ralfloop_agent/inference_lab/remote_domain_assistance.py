from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Callable


@dataclass(frozen=True)
class RemoteDomainAssistanceConfig:
    enabled: bool = False

    @classmethod
    def from_env(cls) -> "RemoteDomainAssistanceConfig":
        return cls(enabled=os.getenv("RALF_REMOTE_DOMAIN_ASSISTANCE_ENABLED", "0") == "1")


def sanitize_domain_advice(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("domain_advice_must_be_object")
    allowed = {
        "intent",
        "domain_candidates",
        "facts",
        "rule_candidates",
        "contradictions",
        "compressed_context",
        "confidence",
    }
    result = {str(key): item for key, item in value.items() if key in allowed}
    if "confidence" in result:
        confidence = result["confidence"]
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
            raise ValueError("invalid_domain_advice_confidence")
    serialized = repr(result).lower()
    forbidden = ("auto_approve", "auto_execute", "promote_domain", "execute_approved", "capability", "shell", "command")
    if any(marker in serialized for marker in forbidden):
        raise ValueError("binding_domain_advice_forbidden")
    return result


class AdvisoryDomainResolver:
    """Local deterministic resolution always wins; remote output is annotation only."""

    def __init__(
        self,
        *,
        local_resolver: Any,
        remote_call: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
        config: RemoteDomainAssistanceConfig | None = None,
    ) -> None:
        self.local_resolver = local_resolver
        self.remote_call = remote_call
        self.config = config or RemoteDomainAssistanceConfig.from_env()

    def resolve(self, goal: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        local = self.local_resolver.resolve(goal, context)
        local_payload = local.to_dict() if hasattr(local, "to_dict") else dict(local)
        output: dict[str, Any] = {
            "resolution": local_payload,
            "resolver_precedence": "local_deterministic",
            "remote_used": False,
            "domain_state_changed": False,
            "promotion_requested": False,
        }
        if not self.config.enabled or self.remote_call is None:
            output["remote_reason"] = "disabled"
            return output
        try:
            raw = self.remote_call(
                "domain_candidate_generation",
                {"goal": goal[:8192], "local_status": local_payload.get("status")},
            )
            output["remote_advice"] = sanitize_domain_advice(raw)
            output["remote_used"] = True
        except Exception as exc:
            output["remote_reason"] = f"unavailable:{type(exc).__name__}"
        return output
