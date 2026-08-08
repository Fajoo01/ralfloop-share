from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ralfloop_agent.integration.capability_adapter import route_task
from ralfloop_agent.models.result_envelope import ResultEnvelope
from ralfloop_agent.nodes.reasoning import run_capability_reasoning_cycle
from src.router import MCP_KEYWORDS, jury_policy_manifest
from src.skills import SkillsRegistry
from src.text_mas_proxy import build_text_mas_trace


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
BRIDGE_VERSION = "0.2.0"


@dataclass(frozen=True)
class CheshireV2BridgeConfig:
    base_url: str = "http://127.0.0.1:1865"
    enabled: bool = False
    timeout_sec: int = 5
    local_only: bool = True
    execute_enabled: bool = False


class CheshireV2BridgeError(RuntimeError):
    pass


class CheshireV2Bridge:
    """Opt-in adapter between Cheshire Cat v2 and Ralfloop.

    Cheshire Cat v2 stays a UI/agent shell. Ralfloop remains the policy,
    skills, evidence and confirmation source of truth.
    """

    def __init__(self, config: CheshireV2BridgeConfig | None = None) -> None:
        self.config = config or CheshireV2BridgeConfig()
        self._assert_local_base_url()

    def openapi_url(self) -> str:
        return f"{self.config.base_url.rstrip('/')}/openapi.json"

    def fetch_openapi(self) -> dict[str, Any]:
        if not self.config.enabled:
            raise CheshireV2BridgeError("cheshire_v2_bridge_disabled")
        request = Request(self.openapi_url(), headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.config.timeout_sec) as response:
                import json

                return json.loads(response.read().decode("utf-8"))
        except URLError as exc:
            raise CheshireV2BridgeError(f"cheshire_v2_openapi_unreachable: {exc}") from exc

    def capability_manifest(self) -> dict[str, Any]:
        skills = SkillsRegistry()
        return {
            "bridge": {
                "name": "ralfloop_cheshire_v2_bridge",
                "version": BRIDGE_VERSION,
                "mode": "opt_in_lab",
                "cheshire_base_url": self.config.base_url,
                "cheshire_openapi_url": self.openapi_url(),
                "enabled": self.config.enabled,
                "execute_enabled": self.config.execute_enabled,
                "local_only": self.config.local_only,
            },
            "contract": {
                "primary": "ResultEnvelope",
                "fields": [
                    "route",
                    "jury_policy",
                    "collaboration_backend",
                    "verification_policy",
                    "collaboration_trace",
                    "evidence",
                    "confirmation",
                    "answer",
                    "meta",
                ],
                "legacy_compatibility": True,
            },
            "agentic_loop": [
                "route_task",
                "reasoning_cycle_node",
                "recursive_mas_jury_loop_when_required",
                "ShellExecutor_or_MCPClient",
                "human_confirmation_gate_if_external",
                "recursive_mas_final_review_when_required",
                "audit_jsonl",
                "ResultEnvelope",
            ],
            "modes": {
                "check_only": "read/audit/diagnosis, no writes",
                "patch_allowed": "sandbox/repo patch after evidence and tests",
                "external_action": "email/telegram/drive/browser side effects behind confirmation",
                "route_only": "dry routing, no execution",
            },
            "skills": [
                {"name": name, "keywords": list(skills.keywords.get(name, ()))}
                for name in sorted(skills.skills)
            ],
            "mcp_connectors": [
                {"name": name, "keywords": list(keywords)}
                for name, keywords in MCP_KEYWORDS
            ],
            "jury": jury_policy_manifest(),
            "collaboration_backends": {
                "single": "routing_only",
                "text_proxy": "text_mas_proxy",
                "recursive_mas_native": "native_latent_only_when_probe_and_execution_succeed",
            },
            "verification": {
                "deterministic": "exit code, policy, tests, thresholds",
                "rule": "numeric or structured thresholds",
                "llm_judge": "qualitative verification only, never side-effect approval",
                "combined": "deterministic/rule first, optional judge later",
            },
            "policy": {
                "skills_before_mcp": True,
                "dynamic_skill_manifests": True,
                "external_actions_require_confirmation": True,
                "external_actions_require_jury": True,
                "no_cheshire_direct_send": True,
                "no_runtime_default_change": True,
                "no_core_write": True,
                "jury_trace_deprecated_alias": True,
            },
        }

    def describe_task(self, user_goal: str) -> dict[str, Any]:
        envelope = self.route_message(user_goal, "route_only")
        payload = self.to_cat_payload(envelope)
        payload["manifest"] = self.capability_manifest()
        return payload

    def route_message(self, user_goal: str, mode: str | None = "route_only") -> ResultEnvelope:
        route = route_task(user_goal, mode if mode != "route_only" else None)
        collaboration_trace = build_text_mas_trace(user_goal, route)
        return ResultEnvelope(
            route=route,
            jury_policy=route.jury_policy,
            collaboration_backend=route.collaboration_backend,
            verification_policy=route.verification_policy,
            collaboration_trace=collaboration_trace,
            jury_trace=collaboration_trace,
            answer="cheshire_v2_route_only",
            meta={
                "source": "cheshire_v2_bridge",
                "bridge_enabled": self.config.enabled,
                "execute_enabled": self.config.execute_enabled,
                "openapi_url": self.openapi_url(),
            },
        )

    def run_message(self, user_goal: str, context: dict[str, Any] | None = None) -> ResultEnvelope:
        if not self.config.execute_enabled:
            envelope = self.route_message(user_goal, "route_only")
            envelope.answer = "cheshire_v2_execute_disabled"
            envelope.meta["blocked_reason"] = "execute_enabled_false"
            return envelope
        return run_capability_reasoning_cycle(
            user_goal,
            {
                **dict(context or {}),
                "source": "cheshire_v2_bridge",
            },
        )

    @staticmethod
    def to_cat_payload(envelope: ResultEnvelope) -> dict[str, Any]:
        payload = envelope.model_dump(mode="json")
        return {
            "answer": envelope.answer,
            "result_envelope": payload,
            "capability_route": payload["route"],
            "evidence": payload.get("evidence"),
            "confirmation": payload.get("confirmation"),
            "jury_policy": payload.get("jury_policy") or payload["route"].get("jury_policy"),
            "collaboration_backend": payload.get("collaboration_backend") or payload["route"].get("collaboration_backend"),
            "verification_policy": payload.get("verification_policy") or payload["route"].get("verification_policy"),
            "collaboration_trace": payload.get("collaboration_trace") or (payload.get("meta") or {}).get("collaboration_trace"),
            "human_confirmation": payload.get("human_confirmation"),
            "jury_trace": payload.get("jury_trace") or payload.get("collaboration_trace") or (payload.get("meta") or {}).get("collaboration_trace"),
            "requires_confirmation": payload["route"].get("requires_confirmation", False),
            "pending_confirmation_id": (
                payload.get("confirmation") or {}
            ).get("id"),
            "agentic_loop": [
                "route_task",
                "reasoning_cycle_node",
                "collaboration_trace",
                "executor_or_mcp",
                "confirmation_gate",
                "audit",
                "result_envelope",
            ],
        }

    def _assert_local_base_url(self) -> None:
        parsed = urlparse(self.config.base_url)
        if parsed.scheme not in {"http", "https"}:
            raise CheshireV2BridgeError("invalid_cheshire_v2_base_url_scheme")
        host = parsed.hostname
        if not host:
            raise CheshireV2BridgeError("invalid_cheshire_v2_base_url_host")
        if self.config.local_only and not _is_local_host(host):
            raise CheshireV2BridgeError("cheshire_v2_base_url_must_be_local")


def _is_local_host(host: str) -> bool:
    if host in LOCAL_HOSTS:
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False
