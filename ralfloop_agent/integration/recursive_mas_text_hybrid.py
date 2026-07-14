from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from typing import Any, Callable


@dataclass(frozen=True)
class RecursiveMASTextHybridConfig:
    enabled: bool = False
    remote_critic_enabled: bool = True
    max_goal_chars: int = 8192

    @classmethod
    def from_env(cls) -> "RecursiveMASTextHybridConfig":
        return cls(
            enabled=os.getenv("RALF_RECURSIVE_HYBRID_ENABLED", "0") == "1",
            remote_critic_enabled=os.getenv("RALF_RECURSIVE_HYBRID_CRITIC_ENABLED", "1") == "1",
        )


@dataclass(frozen=True)
class RemoteCriticResult:
    issues: tuple[str, ...]
    missing_facts: tuple[str, ...]
    format_errors: tuple[str, ...]
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return {**data, "issues": list(self.issues), "missing_facts": list(self.missing_facts), "format_errors": list(self.format_errors)}


def validate_remote_critic(payload: Any) -> RemoteCriticResult:
    if not isinstance(payload, dict) or set(payload) - {"issues", "missing_facts", "format_errors", "confidence"}:
        raise ValueError("invalid_remote_critic_schema")
    lists: list[tuple[str, ...]] = []
    for key in ("issues", "missing_facts", "format_errors"):
        value = payload.get(key, [])
        if not isinstance(value, list) or len(value) > 32 or not all(isinstance(item, str) and len(item) <= 512 for item in value):
            raise ValueError(f"invalid_remote_critic_{key}")
        lists.append(tuple(value))
    confidence = payload.get("confidence", 0.0)
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        raise ValueError("invalid_remote_critic_confidence")
    serialized = repr(payload).lower()
    if any(marker in serialized for marker in ("auto_approve", "auto_execute", "execute_approved", "promote_domain", "shell", "command")):
        raise ValueError("binding_remote_critic_forbidden")
    return RemoteCriticResult(lists[0], lists[1], lists[2], float(confidence))


class RecursiveMASTextHybrid:
    """Text advisory around native RecursiveMAS; latent execution stays on Sibilla."""

    def __init__(
        self,
        *,
        native_execute: Callable[[dict[str, Any]], dict[str, Any]],
        remote_call: Callable[[str, dict[str, Any]], dict[str, Any]] | None,
        deterministic_verify: Callable[[str, dict[str, Any]], dict[str, Any]],
        config: RecursiveMASTextHybridConfig | None = None,
    ) -> None:
        self.native_execute = native_execute
        self.remote_call = remote_call
        self.deterministic_verify = deterministic_verify
        self.config = config or RecursiveMASTextHybridConfig.from_env()

    def execute(self, request: dict[str, Any]) -> dict[str, Any]:
        if not self.config.enabled:
            return {
                "ok": False,
                "status": "disabled",
                "requested_backend": "recursive_mas_text_hybrid",
                "selected_backend": "none",
                "native_unchanged": True,
            }
        if request.get("side_effect") or request.get("requires_human_confirmation"):
            return {
                "ok": False,
                "status": "human_confirmation_required",
                "requested_backend": "recursive_mas_text_hybrid",
                "selected_backend": "none",
                "native_unchanged": True,
            }
        goal = str(request.get("goal") or request.get("question") or "")[: self.config.max_goal_chars]
        advisory: dict[str, Any] = {}
        remote_error: str | None = None
        if self.remote_call is not None:
            try:
                advisory = self.remote_call("context_compression", {"goal": goal})
            except Exception as exc:
                remote_error = f"preprocess:{type(exc).__name__}"
        native_request = dict(request)
        native_request["goal"] = goal
        native_request["hybrid_advisory"] = {
            "untrusted": True,
            "facts": advisory.get("facts", []) if isinstance(advisory, dict) else [],
            "compressed_context": advisory.get("compressed_context", "") if isinstance(advisory, dict) else "",
            "domain_candidates": advisory.get("domain_candidates", []) if isinstance(advisory, dict) else [],
        }
        native = self.native_execute(native_request)
        answer = str(native.get("answer") or "")
        critic: RemoteCriticResult | None = None
        if self.config.remote_critic_enabled and self.remote_call is not None and answer:
            try:
                critic = validate_remote_critic(self.remote_call("structured_critic", {"goal": goal, "answer": answer[:8192]}))
            except Exception as exc:
                remote_error = f"critic:{type(exc).__name__}"
        verification = self.deterministic_verify(answer, request)
        return {
            **native,
            "requested_backend": "recursive_mas_text_hybrid",
            "selected_backend": "recursive_mas_text_hybrid",
            "implementation_level": "native_latent_plus_text_advisory",
            "native_backend": native.get("selected_backend", "recursive_mas_native"),
            "native_unchanged": True,
            "remote_preprocess_used": bool(advisory),
            "remote_critic": critic.to_dict() if critic else None,
            "remote_critic_binding": False,
            "remote_rewrites_answer": False,
            "deterministic_verification": verification,
            "remote_error": remote_error,
        }
