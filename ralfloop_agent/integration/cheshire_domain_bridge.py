from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Any

from ralfloop_agent.domains.deterministic_engine import DeterministicEngine
from ralfloop_agent.domains.jury_router import DomainJuryRouter
from ralfloop_agent.domains.registry import DomainRegistry
from ralfloop_agent.domains.resolver import DomainResolver
from ralfloop_agent.domains.storage import append_jsonl, now_iso
from ralfloop_agent.integration.recursive_mas_host_client import PROTOCOL_VERSION, RecursiveMASHostClient


DOMAIN_MISSING_MESSAGE = (
    "Non esiste ancora un dominio validato per questa richiesta. "
    "Serve creare e approvare manuale, regole, fonti e test."
)
SECRET_MARKERS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "Authorization", "Bearer")


@dataclass
class CheshireDomainBridgeConfig:
    enabled: bool = False
    enable_domain_jury: bool = False
    enable_recursive_mas_native: bool = False
    allow_legacy_fallback: bool = True
    timeout_sec: int = 150
    fail_open: bool = True
    audit_enabled: bool = True
    max_context_chars: int = 24000
    recursive_mas_transport: str = "local_controller"
    recursive_mas_socket: str = "/run/ralfloop/recursive-mas.sock"

    @classmethod
    def from_env(cls) -> "CheshireDomainBridgeConfig":
        return cls(
            enabled=os.getenv("RALFLOOP_ENABLE_DOMAIN_ORCHESTRATOR", "0") == "1",
            enable_domain_jury=os.getenv("RALFLOOP_ENABLE_DOMAIN_JURY", "0") == "1",
            enable_recursive_mas_native=os.getenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "0") == "1",
            allow_legacy_fallback=os.getenv("RALFLOOP_ALLOW_LEGACY_FALLBACK", "1") == "1",
            timeout_sec=int(os.getenv("RALFLOOP_DOMAIN_BRIDGE_TIMEOUT_SEC", "150")),
            fail_open=os.getenv("RALFLOOP_DOMAIN_BRIDGE_FAIL_OPEN", "1") == "1",
            audit_enabled=os.getenv("RALFLOOP_DOMAIN_BRIDGE_AUDIT", "1") == "1",
            max_context_chars=int(os.getenv("RALFLOOP_DOMAIN_CONTEXT_MAX_CHARS", "24000")),
            recursive_mas_transport=os.getenv("RALFLOOP_RECURSIVE_MAS_TRANSPORT", "local_controller"),
            recursive_mas_socket=os.getenv("RALFLOOP_RECURSIVE_MAS_SOCKET", "/run/ralfloop/recursive-mas.sock"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheshireDomainBridgeRequest:
    request_id: str
    user_id_hash: str
    message: str
    conversation_id_hash: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    requested_domain: str | None = None
    side_effect_intent: bool = False

    @classmethod
    def from_message(cls, message: str, context: dict[str, Any] | None = None) -> "CheshireDomainBridgeRequest":
        context = context or {}
        return cls(
            request_id=str(context.get("request_id") or uuid.uuid4()),
            user_id_hash=_hash_value(context.get("user_id") or context.get("user_id_hash") or ""),
            message=str(message or ""),
            conversation_id_hash=_hash_value(context.get("conversation_id") or context.get("conversation_id_hash") or ""),
            attachments=list(context.get("attachments") or []),
            metadata=dict(context.get("metadata") or {}),
            requested_domain=context.get("domain") or context.get("requested_domain"),
            side_effect_intent=bool(context.get("side_effect_intent", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CheshireDomainBridgeResult:
    ok: bool
    status: str
    route: str
    domain_resolution: dict[str, Any] = field(default_factory=dict)
    classification: str | None = None
    deterministic_result: dict[str, Any] = field(default_factory=dict)
    jury_required: bool = False
    jury_status: str | None = None
    jury_result: dict[str, Any] = field(default_factory=dict)
    answer: str | None = None
    legacy_fallback_required: bool = False
    fallback_reason: str | None = None
    human_confirmation_required: bool = False
    domain_creation_required: bool = False
    duration_ms: int = 0
    error_type: str | None = None
    error: str | None = None
    audit_id: str | None = None
    domain_id: str | None = None
    domain_version: str | None = None
    source: str | None = None
    native_latent_verified: bool = False
    limitations: list[str] = field(default_factory=list)
    context_bundle: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CheshireDomainBridge:
    def __init__(
        self,
        config: CheshireDomainBridgeConfig | None = None,
        registry: DomainRegistry | None = None,
        runtime_controller: Any | None = None,
    ) -> None:
        self.config = config or CheshireDomainBridgeConfig.from_env()
        self.registry = registry or DomainRegistry()
        self.runtime_controller = runtime_controller

    @classmethod
    def from_env(cls) -> "CheshireDomainBridge":
        return cls(CheshireDomainBridgeConfig.from_env())

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "feature_enabled": self.config.enabled,
            "domain_jury_enabled": self.config.enable_domain_jury,
            "recursive_mas_native_enabled": self.config.enable_recursive_mas_native,
            "legacy_fallback_enabled": self.config.allow_legacy_fallback,
            "recursive_mas_transport": self.config.recursive_mas_transport,
        }

    def health(self) -> dict[str, Any]:
        status = self.status()
        status["route"] = "domain_orchestrator" if self.config.enabled else "legacy"
        return status

    def route_message(self, request: CheshireDomainBridgeRequest | str, context: dict[str, Any] | None = None) -> CheshireDomainBridgeResult:
        req = request if isinstance(request, CheshireDomainBridgeRequest) else CheshireDomainBridgeRequest.from_message(str(request), context)
        if not self.config.enabled:
            return self._legacy(req, "domain_orchestrator_disabled", 0, str(uuid.uuid4()))
        return self.execute(req)

    def execute(self, request: CheshireDomainBridgeRequest | dict[str, Any]) -> CheshireDomainBridgeResult:
        started = time.monotonic()
        audit_id = str(uuid.uuid4())
        req = _coerce_request(request)
        if not self.config.enabled:
            result = self._legacy(req, "domain_orchestrator_disabled", started, audit_id)
            self._audit(req, result)
            return result
        try:
            result = self._execute_enabled(req, started, audit_id)
        except Exception as exc:
            result = self._error_or_legacy(req, started, audit_id, type(exc).__name__, str(exc))
        self._audit(req, result)
        return result

    def to_cheshire_response(self, result: CheshireDomainBridgeResult | dict[str, Any]) -> dict[str, Any]:
        data = result.to_dict() if isinstance(result, CheshireDomainBridgeResult) else result
        status = data.get("status")
        if data.get("legacy_fallback_required"):
            return {"handled": False, "legacy_fallback_required": True, "fallback_reason": data.get("fallback_reason")}
        if status == "completed" and data.get("source") == "domain_deterministic":
            return {
                "handled": True,
                "answer": data.get("answer"),
                "source": "domain_deterministic",
                "domain_id": data.get("domain_id"),
                "domain_version": data.get("domain_version"),
                "jury_used": False,
            }
        if status == "completed" and data.get("source") == "domain_jury_recursive_mas":
            return {
                "handled": True,
                "answer": data.get("answer"),
                "source": "domain_jury_recursive_mas",
                "domain_id": data.get("domain_id"),
                "domain_version": data.get("domain_version"),
                "jury_used": True,
                "native_latent_verified": bool(data.get("native_latent_verified")),
                "limitations": data.get("limitations", []),
            }
        if data.get("domain_creation_required"):
            return {"handled": True, "answer": DOMAIN_MISSING_MESSAGE, "source": "domain_missing", "domain_creation_required": True}
        if data.get("human_confirmation_required"):
            return {"handled": True, "answer": "Serve conferma umana prima di qualunque azione esterna.", "source": "human_confirmation_required", "human_confirmation_required": True}
        if status == "jury_required":
            return {
                "handled": True,
                "answer": "Serve la giuria di dominio, ma al momento non è abilitata.",
                "source": "domain_jury_required",
                "domain_id": data.get("domain_id"),
                "domain_version": data.get("domain_version"),
                "jury_used": False,
                "jury_status": data.get("jury_status"),
            }
        if status in {"jury_timeout", "jury_busy", "circuit_open", "jury_failed"}:
            return {
                "handled": True,
                "answer": "La giuria di dominio non ha completato la valutazione. Nessuna azione è stata eseguita.",
                "source": status,
                "jury_used": False,
            }
        return {"handled": False, "legacy_fallback_required": True, "fallback_reason": data.get("fallback_reason") or status or "unhandled"}

    def _execute_enabled(self, req: CheshireDomainBridgeRequest, started: float, audit_id: str) -> CheshireDomainBridgeResult:
        resolution = DomainResolver(self.registry).resolve(req.message, {"domain_id": req.requested_domain} if req.requested_domain else None).to_dict()
        if resolution["status"] == "missing":
            return CheshireDomainBridgeResult(True, "domain_creation_required", "domain_orchestrator", domain_resolution=resolution, domain_creation_required=True, answer=DOMAIN_MISSING_MESSAGE, duration_ms=_elapsed_ms(started), audit_id=audit_id, source="domain_missing")
        if resolution["status"] == "ambiguous":
            return CheshireDomainBridgeResult(True, "jury_required", "domain_orchestrator", domain_resolution=resolution, jury_required=True, jury_status="domain_selection_required", duration_ms=_elapsed_ms(started), audit_id=audit_id)
        domain = self.registry.find_active(str(resolution.get("domain_id")))
        if not domain or domain["manifest"].get("state") != "active":
            return self._legacy(req, "domain_not_active", started, audit_id, resolution)
        integrity = self.registry.verify_integrity(domain["manifest"]["domain_id"], domain["manifest"]["version"])
        if not integrity.get("ok"):
            return self._legacy(req, "domain_integrity_failed", started, audit_id, resolution)
        engine = DeterministicEngine()
        classification = "external_action" if req.side_effect_intent else engine.classify(domain, req.message)
        deterministic = engine.evaluate(domain, req.message)
        if classification == "external_action":
            return CheshireDomainBridgeResult(True, "human_confirmation_required", "domain_orchestrator", domain_resolution=resolution, classification=classification, deterministic_result=deterministic, human_confirmation_required=True, duration_ms=_elapsed_ms(started), audit_id=audit_id, domain_id=domain["manifest"]["domain_id"], domain_version=domain["manifest"]["version"], source="human_confirmation_required")
        if deterministic.get("complete") and not deterministic.get("blocking_errors"):
            return CheshireDomainBridgeResult(True, "completed", "domain_orchestrator", domain_resolution=resolution, classification=classification, deterministic_result=deterministic, answer=str(deterministic.get("result")), duration_ms=_elapsed_ms(started), audit_id=audit_id, domain_id=domain["manifest"]["domain_id"], domain_version=domain["manifest"]["version"], source="domain_deterministic")
        jury = DomainJuryRouter().should_use_jury(domain_resolution=resolution, classification=classification, deterministic_result=deterministic)
        if not jury.get("use_jury"):
            return self._legacy(req, "domain_incomplete_no_jury_route", started, audit_id, resolution)
        bundle = build_domain_context_bundle(domain, deterministic, jury, req.message, self.config.max_context_chars)
        if not (self.config.enable_domain_jury and self.config.enable_recursive_mas_native):
            return CheshireDomainBridgeResult(True, "jury_required", "domain_orchestrator", domain_resolution=resolution, classification=classification, deterministic_result=deterministic, jury_required=True, jury_status="disabled", context_bundle=bundle, duration_ms=_elapsed_ms(started), audit_id=audit_id, domain_id=domain["manifest"]["domain_id"], domain_version=domain["manifest"]["version"])
        runtime = self.runtime_controller or self._runtime_backend()
        if self.config.recursive_mas_transport == "host_socket":
            runtime_request = {
                "protocol_version": PROTOCOL_VERSION,
                "request_id": req.request_id,
                "goal": req.message,
                "domain_context": bundle,
                "style": "sequential_light",
                "rounds": int(jury.get("rounds") or 1),
                "requires_human_confirmation": req.side_effect_intent,
            }
        else:
            runtime_request = {"goal": json.dumps(bundle, ensure_ascii=False), "rounds": int(jury.get("rounds") or 1), "profile": "deterministic_diagnostic"}
        jury_result = self._execute_runtime_with_timeout(runtime, runtime_request)
        status = str(jury_result.get("status") or "")
        if status == "busy":
            return CheshireDomainBridgeResult(False, "jury_busy", "domain_orchestrator", domain_resolution=resolution, classification=classification, deterministic_result=deterministic, jury_required=True, jury_status="busy", jury_result=jury_result, context_bundle=bundle, duration_ms=_elapsed_ms(started), audit_id=audit_id, domain_id=domain["manifest"]["domain_id"], domain_version=domain["manifest"]["version"])
        if status in {"timeout", "bridge_timeout"}:
            return CheshireDomainBridgeResult(False, "jury_timeout", "domain_orchestrator", domain_resolution=resolution, classification=classification, deterministic_result=deterministic, jury_required=True, jury_status="timeout", jury_result=jury_result, context_bundle=bundle, duration_ms=_elapsed_ms(started), audit_id=audit_id, domain_id=domain["manifest"]["domain_id"], domain_version=domain["manifest"]["version"], error_type="timeout")
        if not jury_result.get("ok"):
            return CheshireDomainBridgeResult(False, status or "jury_failed", "domain_orchestrator", domain_resolution=resolution, classification=classification, deterministic_result=deterministic, jury_required=True, jury_status=status or "failed", jury_result=jury_result, context_bundle=bundle, duration_ms=_elapsed_ms(started), audit_id=audit_id, domain_id=domain["manifest"]["domain_id"], domain_version=domain["manifest"]["version"], error_type=jury_result.get("error_type"), error=jury_result.get("error"))
        return CheshireDomainBridgeResult(True, "completed", "domain_orchestrator", domain_resolution=resolution, classification=classification, deterministic_result=deterministic, jury_required=True, jury_status="completed", jury_result=jury_result, answer=jury_result.get("answer"), duration_ms=_elapsed_ms(started), audit_id=audit_id, domain_id=domain["manifest"]["domain_id"], domain_version=domain["manifest"]["version"], source="domain_jury_recursive_mas", native_latent_verified=bool(jury_result.get("native_latent_verified")), limitations=["jury_interpretation_not_fact"], context_bundle=bundle)

    def _runtime_backend(self) -> Any:
        if self.config.recursive_mas_transport == "host_socket":
            return RecursiveMASHostClient.from_env()
        from ralfloop_agent.integration.recursive_mas_runtime import RecursiveMASRuntimeController

        return RecursiveMASRuntimeController.from_env()

    def _execute_runtime_with_timeout(self, runtime: Any, request: dict[str, Any]) -> dict[str, Any]:
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(runtime.execute, request)
        try:
            result = future.result(timeout=self.config.timeout_sec)
        except FutureTimeout:
            future.cancel()
            pool.shutdown(wait=False, cancel_futures=True)
            return {"ok": False, "status": "bridge_timeout", "error_type": "timeout", "error": "Domain bridge timeout"}
        finally:
            if future.done():
                pool.shutdown(wait=False, cancel_futures=True)
        return result if isinstance(result, dict) else {"ok": False, "status": "invalid_runtime_result"}

    def _legacy(self, req: CheshireDomainBridgeRequest, reason: str, started: float, audit_id: str, resolution: dict[str, Any] | None = None) -> CheshireDomainBridgeResult:
        return CheshireDomainBridgeResult(True, "legacy", "legacy", domain_resolution=resolution or {}, legacy_fallback_required=True, fallback_reason=reason, duration_ms=_elapsed_ms(started), audit_id=audit_id)

    def _error_or_legacy(self, req: CheshireDomainBridgeRequest, started: float, audit_id: str, error_type: str, error: str) -> CheshireDomainBridgeResult:
        if self.config.fail_open and self.config.allow_legacy_fallback:
            return CheshireDomainBridgeResult(False, "legacy", "legacy", legacy_fallback_required=True, fallback_reason="bridge_error", duration_ms=_elapsed_ms(started), audit_id=audit_id, error_type=error_type, error=error)
        return CheshireDomainBridgeResult(False, "error", "domain_orchestrator", duration_ms=_elapsed_ms(started), audit_id=audit_id, error_type=error_type, error=error)

    def _audit(self, req: CheshireDomainBridgeRequest, result: CheshireDomainBridgeResult) -> None:
        if not self.config.audit_enabled:
            return
        record = {
            "timestamp": now_iso(),
            "audit_id": result.audit_id,
            "request_id": req.request_id,
            "message_hash": hashlib.sha256(req.message.encode("utf-8")).hexdigest(),
            "message_preview_redacted": _redact(req.message)[:120],
            "feature_enabled": self.config.enabled,
            "domain_resolution": result.domain_resolution.get("status"),
            "domain_id": result.domain_id,
            "domain_version": result.domain_version,
            "classification": result.classification,
            "deterministic_complete": bool(result.deterministic_result.get("complete")),
            "rules_used": result.deterministic_result.get("matched_rules", []),
            "jury_required": result.jury_required,
            "jury_invoked": bool(result.jury_result),
            "jury_backend": result.jury_result.get("selected_backend") if result.jury_result else None,
            "native_latent_verified": result.native_latent_verified,
            "legacy_fallback": result.legacy_fallback_required,
            "fallback_reason": result.fallback_reason,
            "human_confirmation_required": result.human_confirmation_required,
            "domain_creation_required": result.domain_creation_required,
            "duration_ms": result.duration_ms,
            "error_type": result.error_type,
            "error": _redact(str(result.error or ""))[:1000],
        }
        try:
            append_jsonl(Path("logs/cheshire_domain_bridge.jsonl"), record)
        except PermissionError:
            append_jsonl(Path(".ralf_run/cheshire_domain_bridge.jsonl"), record)


def build_domain_context_bundle(domain: dict[str, Any], deterministic: dict[str, Any], jury: dict[str, Any], question: str, max_chars: int = 24000) -> dict[str, Any]:
    manifest = domain["manifest"]
    conflicts = domain.get("conflicts", [])
    bundle = {
        "domain_id": manifest.get("domain_id"),
        "domain_version": manifest.get("version"),
        "manual_sections": [],
        "glossary_terms": [],
        "matched_rules": deterministic.get("matched_rules", []),
        "deterministic_facts": deterministic.get("facts", {}),
        "deterministic_conclusions": deterministic.get("derived_values", {}),
        "unresolved_questions": deterministic.get("unresolved_questions", []),
        "conflicting_sources": conflicts[:5],
        "exceptions": [],
        "evidence_refs": deterministic.get("evidence_refs", []),
        "out_of_scope": manifest.get("out_of_scope", []),
        "answer_constraints": [
            "Do not change deterministic conclusions.",
            "Separate facts, interpretations, hypotheses, recommendations, unknowns.",
            "Do not authorize side effects.",
        ],
        "question": question[:2000],
        "jury_reason_codes": jury.get("reason_codes", []),
    }
    text = json.dumps(bundle, ensure_ascii=False)
    if len(text) > max_chars:
        bundle["question"] = bundle["question"][:500]
        bundle["conflicting_sources"] = bundle["conflicting_sources"][:2]
        bundle["answer_constraints"] = bundle["answer_constraints"][:2]
    return bundle


def _coerce_request(raw: CheshireDomainBridgeRequest | dict[str, Any]) -> CheshireDomainBridgeRequest:
    if isinstance(raw, CheshireDomainBridgeRequest):
        return raw
    return CheshireDomainBridgeRequest(
        request_id=str(raw.get("request_id") or uuid.uuid4()),
        user_id_hash=_hash_value(raw.get("user_id_hash") or raw.get("user_id") or ""),
        message=str(raw.get("message") or raw.get("text") or ""),
        conversation_id_hash=_hash_value(raw.get("conversation_id_hash") or raw.get("conversation_id") or ""),
        attachments=list(raw.get("attachments") or []),
        metadata=dict(raw.get("metadata") or {}),
        requested_domain=raw.get("requested_domain") or raw.get("domain"),
        side_effect_intent=bool(raw.get("side_effect_intent", False)),
    )


def _hash_value(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if re.fullmatch(r"[0-9a-f]{64}", text):
        return text
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact(text: str) -> str:
    out = str(text or "")
    out = re.sub(r"(Authorization)\s*[:=]?\s*\S+", r"\1<redacted>", out, flags=re.IGNORECASE)
    out = re.sub(r"(Bearer)\s+\S+", r"\1<redacted>", out, flags=re.IGNORECASE)
    out = re.sub(r"(OPENAI_API_KEY|ANTHROPIC_API_KEY)\s*[:=]?\s*\S+", r"\1<redacted>", out)
    for marker in SECRET_MARKERS:
        out = out.replace(marker, f"{marker[:4]}<redacted>")
    return out


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000) if started else 0
