from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from ralfloop_agent.domains.capability_registry import CanonicalCapabilityRegistry
from ralfloop_agent.local_arch.router import ToolRegistry
from ralfloop_agent.model_tools.registry import ModelToolRegistry
from src.skills import SkillsRegistry

from .contracts import (
    DomainSpec,
    MemoryNamespace,
    PolicyClass,
    SkillSpec,
    UnifiedToolSpec,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FACADE = PROJECT_ROOT / "config" / "unified_assistant_domains_v1.json"
DEFAULT_LOCAL_TOOLS = PROJECT_ROOT / "config" / "local_arch_tools_v1.json"
DEFAULT_CAPABILITIES = PROJECT_ROOT / "config" / "domain_capability_mapping.yaml"
DEFAULT_MODEL_TOOLS = PROJECT_ROOT / "config" / "model_tools.json"
DEFAULT_HOME_ENTITIES = PROJECT_ROOT / "config" / "home_entities_v1.json"
DEFAULT_SEMANTIC_JUDGE = PROJECT_ROOT / "config" / "semantic_judge.json"
DEFAULT_WHATSAPP_SCOPES = PROJECT_ROOT / "config" / "whatsapp_memory_scopes_v1.json"
DEFAULT_DOMAIN_REGISTRY = PROJECT_ROOT / "domains" / "registry.json"


class UnifiedRegistryFacade:
    """Read-only facade; existing registries remain authoritative."""

    def __init__(
        self,
        *,
        facade_path: str | Path = DEFAULT_FACADE,
        local_tools_path: str | Path = DEFAULT_LOCAL_TOOLS,
        capabilities_path: str | Path = DEFAULT_CAPABILITIES,
        model_tools_path: str | Path = DEFAULT_MODEL_TOOLS,
        home_entities_path: str | Path = DEFAULT_HOME_ENTITIES,
        semantic_judge_path: str | Path = DEFAULT_SEMANTIC_JUDGE,
        whatsapp_scopes_path: str | Path = DEFAULT_WHATSAPP_SCOPES,
        domain_registry_path: str | Path = DEFAULT_DOMAIN_REGISTRY,
    ) -> None:
        self.facade_path = Path(facade_path)
        self.local_tools_path = Path(local_tools_path)
        self.capabilities_path = Path(capabilities_path)
        self.model_tools_path = Path(model_tools_path)
        self.home_entities_path = Path(home_entities_path)
        self.semantic_judge_path = Path(semantic_judge_path)
        self.whatsapp_scopes_path = Path(whatsapp_scopes_path)
        self.domain_registry_path = Path(domain_registry_path)
        payload = json.loads(self.facade_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("invalid_unified_registry_facade")
        self.source_registries = tuple(str(value) for value in payload.get("source_registries", ()))
        self.domains = _index_unique(
            (DomainSpec.model_validate(item) for item in payload.get("domains", ())), "id"
        )
        self.skills = _index_unique(
            (SkillSpec.model_validate(item) for item in payload.get("skills", ())), "id"
        )
        self.local_tools = ToolRegistry.load(self.local_tools_path)
        self.canonical = CanonicalCapabilityRegistry.from_mapping(self.capabilities_path)
        self.model_tools = ModelToolRegistry.load(self.model_tools_path)
        self.existing_skills = SkillsRegistry()
        self.existing_domain_records = self._load_existing_domain_records()
        for skill in self._adapt_existing_skills():
            self.skills.setdefault(skill.id, skill)
        self._validate_references()

    def domain(self, value: str) -> DomainSpec:
        folded = value.strip().casefold()
        matches = [
            spec for spec in self.domains.values()
            if folded == spec.id or folded in {alias.casefold() for alias in spec.aliases}
        ]
        if len(matches) != 1:
            raise KeyError("domain_unresolved" if not matches else "domain_ambiguous")
        return matches[0]

    def skill(self, skill_id: str) -> SkillSpec:
        try:
            return self.skills[skill_id]
        except KeyError as exc:
            raise KeyError("skill_unresolved") from exc

    def list_tools(self) -> tuple[UnifiedToolSpec, ...]:
        rows: list[UnifiedToolSpec] = []
        for tool in self.local_tools.tools:
            classification = _local_classification(tool.approval_requirement, tool.side_effect)
            rows.append(UnifiedToolSpec(
                id=tool.name,
                capabilities=tool.capabilities,
                input_schema=f"sha256:{tool.input_schema_hash}",
                output_schema=f"sha256:{tool.output_schema_hash}",
                classification=classification,
                side_effect_class=_side_effect_label(classification),
                availability=tool.availability,
                health=f"registry:{tool.availability}",
                verification_method=tool.provenance_policy,
                source_registry=str(self.local_tools_path),
            ))
        for cap in self.canonical.capabilities.values():
            classification = PolicyClass.PROTECTED if cap.side_effects else PolicyClass.READ
            rows.append(UnifiedToolSpec(
                id=cap.capability_id,
                capabilities=(cap.capability_id,),
                input_schema="required:" + ",".join(cap.required_inputs or ("none",)),
                output_schema="fields:" + ",".join(cap.output_schema.get("fields", ()) or ("unspecified",)),
                classification=classification,
                side_effect_class=_side_effect_label(classification),
                availability="available" if cap.enabled else "disabled",
                health="source_checksum_validation",
                verification_method="canonical executor + source checksums",
                source_registry=str(self.capabilities_path),
            ))
        for spec in self.model_tools.specs.values():
            status = self.model_tools.availability(spec)
            classification = (
                PolicyClass.PROTECTED
                if spec.capability == "sandboxed_remote_code"
                else PolicyClass.READ
            )
            rows.append(UnifiedToolSpec(
                id=spec.tool_id,
                capabilities=(spec.capability,),
                input_schema=_schema_digest(spec.input_schema),
                output_schema=_schema_digest(spec.output_schema),
                classification=classification,
                side_effect_class=_side_effect_label(classification),
                availability=status.status,
                health=status.status,
                verification_method="strict schema + ModelToolEnvelope provenance",
                source_registry=str(self.model_tools_path),
            ))
        rows.extend(self._runtime_adapters())
        return tuple(_dedupe_tools(rows))

    def capability_map(self) -> list[dict[str, Any]]:
        tools = {tool.id: tool for tool in self.list_tools()}
        rows: list[dict[str, Any]] = []
        for domain in sorted(self.domains.values(), key=lambda item: item.id):
            for skill_id in domain.supported_skills:
                skill = self.skills.get(skill_id)
                rows.append({
                    "domain": domain.id,
                    "skill": skill_id,
                    "status": skill.status if skill else "unregistered",
                    "policy": skill.classification if skill else PolicyClass.DENY,
                    "capabilities": list(skill.required_capabilities) if skill else [],
                    "available_tools": sorted(
                        tool.id for tool in tools.values()
                        if skill and set(tool.capabilities) & set(skill.required_capabilities)
                    ),
                    "source_refs": list(domain.source_refs),
                })
        return rows

    def inventory(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "source_registries": list(self.source_registries),
            "domains": [item.model_dump(mode="json") for item in sorted(self.domains.values(), key=lambda row: row.id)],
            "skills": [item.model_dump(mode="json") for item in sorted(self.skills.values(), key=lambda row: row.id)],
            "tools": [item.model_dump(mode="json") for item in self.list_tools()],
            "capability_map": self.capability_map(),
            "existing_domains": list(self.existing_domain_records),
            "source_of_truth": {
                "home_current_state": "Home Assistant readback",
                "email_facts": "thread/document/domain evidence",
                "tiremm": "verified documents/decisions/email",
                "fastweb_portal_live_state": "authenticated Fastweb portal accessibility snapshot",
                "whatsapp_messages": "authenticated work-profile MCP evidence; DOCUMENT/EPISODIC data only",
                "personal_relational": "observations/reported statements > hypotheses > calculated scores",
            },
        }

    def _runtime_adapters(self) -> list[UnifiedToolSpec]:
        entities = json.loads(self.home_entities_path.read_text(encoding="utf-8"))
        entity_count = len(entities.get("entities") or [])
        home_status = "available" if entity_count else "constrained:no_registered_entities"
        gmail_socket = Path("/run/ralf-google-workspace-mcp/mcp.sock")
        whatsapp_scopes = json.loads(self.whatsapp_scopes_path.read_text(encoding="utf-8"))
        whatsapp_work_profile = (
            whatsapp_scopes.get("profile") == "work"
            and whatsapp_scopes.get("default_namespace") == "tiremm"
        )
        whatsapp_socket = Path("/run/ralf-whatsapp-mcp/mcp.sock")
        mailchimp_socket = Path("/run/ralf-mailchimp-mcp/mcp.sock")
        rows = [UnifiedToolSpec(
            id="google_workspace.gmail.read_only",
            capabilities=("google_workspace.gmail.search", "google_workspace.gmail.read", "google_workspace.gmail.thread"),
            input_schema="EmailSearchIntent",
            output_schema="EmailSearchResult",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability="available" if gmail_socket.exists() else "constrained:broker_unavailable",
            health="validated MCP tool schema + broker socket",
            verification_method="search/read-only allowlist + Gmail message provenance",
            source_registry=str(PROJECT_ROOT / "src" / "google_workspace.py"),
        ), UnifiedToolSpec(
            id="fastweb.portal.read_only",
            capabilities=("fastweb.portal.read_only",),
            input_schema="FastwebPortalReadRequest",
            output_schema="FastwebPortalResult",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability="available",
            health="loopback CDP current-page snapshot",
            verification_method="fixed Accessibility.getFullAXTree; no navigation/click/eval",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "fastweb_portal.py"),
        ), UnifiedToolSpec(
            id="whatsapp.web.mcp",
            capabilities=("whatsapp.mcp.read", "whatsapp.web.media.read"),
            input_schema="strict semantic WhatsApp MCP schemas",
            output_schema="verified WhatsApp MCP result contracts",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability=(
                "available" if whatsapp_socket.exists() and whatsapp_work_profile
                else "constrained:broker_not_started"
            ),
            health="Unix MCP broker + strict tool discovery",
            verification_method="read side-effects=0; send/reply approval CAS + exact outbound verification",
            source_registry=str(PROJECT_ROOT / "src" / "whatsapp.py"),
        ), UnifiedToolSpec(
            id="whatsapp.web.mcp.write",
            capabilities=("whatsapp.mcp.send", "whatsapp.mcp.reply"),
            input_schema="approved exact WhatsApp scope",
            output_schema="verified send/reply result",
            classification=PolicyClass.CONFIRM_WRITE,
            side_effect_class="confirmation_required",
            availability=(
                "available" if whatsapp_socket.exists() and whatsapp_work_profile
                else "constrained:broker_not_started"
            ),
            health="Unix MCP broker + approval store",
            verification_method="hash/version/chat/message binding + CAS + outbound readback",
            source_registry=str(PROJECT_ROOT / "src" / "whatsapp.py"),
        ), UnifiedToolSpec(
            id="mailchimp.marketing.read_only",
            capabilities=(
                "mailchimp.ping",
                "mailchimp.audiences.read",
                "mailchimp.campaigns.read",
            ),
            input_schema="strict semantic Mailchimp MCP schemas",
            output_schema="verified Mailchimp MCP result contracts",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability=(
                "available"
                if mailchimp_socket.exists()
                else "constrained:broker_unavailable"
            ),
            health="Unix MCP broker + strict tool discovery",
            verification_method="read side_effects=0 + writes=0 + sends=0",
            source_registry=str(PROJECT_ROOT / "src" / "mailchimp.py"),
        ), UnifiedToolSpec(
            id="home_assistant.adapter",
            capabilities=("home.state.read", "home.service.call"),
            input_schema="HomeCommand",
            output_schema="HomeExecutionResult",
            classification=PolicyClass.PROTECTED,
            side_effect_class="mixed_read_and_policy_gated_write",
            availability=home_status,
            health="provider read + entity registry",
            verification_method="read before + write + read after",
            source_registry=str(self.home_entities_path),
        )]
        if self.semantic_judge_path.exists():
            raw = json.loads(self.semantic_judge_path.read_text(encoding="utf-8"))
            runtime = raw.get("deepseek_runtime") or {}
            rows.append(UnifiedToolSpec(
                id="deepseek_v4_flash.semantic_critic",
                capabilities=("semantic_critic",),
                input_schema="email_reply_domain_v1 + draft",
                output_schema="SemanticCriticReview",
                classification=PolicyClass.READ,
                side_effect_class="none",
                availability="configured_disabled" if not raw.get("semantic_judge_enabled") else "available",
                health="TransactionalGpuScheduler + DS4 loopback health",
                verification_method=f"strict parser; HIGH_ONLY; max_output={runtime.get('max_output_tokens', 128)}",
                source_registry=str(self.semantic_judge_path),
            ))
        return rows

    def _adapt_existing_skills(self) -> tuple[SkillSpec, ...]:
        rows: list[SkillSpec] = []
        for raw in self.existing_skills.specs.values():
            domains = _external_skill_domains(raw.name)
            classification = _external_skill_policy(raw.name)
            runtime = str(raw.runtime_module or raw.source)
            runtime_path = Path(runtime) if runtime.startswith("/") else None
            status = "ready" if raw.enabled and (runtime_path is None or runtime_path.exists()) else "constrained"
            rows.append(SkillSpec(
                id=raw.name,
                domains=domains,
                required_arguments=("user_goal",),
                required_capabilities=(),
                classification=classification,
                workflow=runtime[:240] or "manifest_only",
                verification_method="existing skill result + declared source",
                status=status,
            ))
        return tuple(rows)

    def _load_existing_domain_records(self) -> tuple[dict[str, Any], ...]:
        raw = json.loads(self.domain_registry_path.read_text(encoding="utf-8"))
        rows: list[dict[str, Any]] = []
        for item in raw.get("domains") or []:
            if not isinstance(item, dict):
                continue
            state = str(item.get("state") or "missing")
            rows.append({
                "id": str(item.get("domain_id") or ""),
                "type": "versioned_domain",
                "path": str(item.get("path") or ""),
                "version": str(item.get("version") or ""),
                "state": state,
                "routing_eligible": state == "active",
                "status": "ready" if state == "active" else "experimental",
                "source_registry": str(self.domain_registry_path),
            })
        return tuple(sorted(rows, key=lambda row: row["id"]))

    def _validate_references(self) -> None:
        for skill in self.skills.values():
            unknown = set(skill.domains) - set(self.domains)
            if unknown:
                raise ValueError(f"skill_unknown_domains:{skill.id}:{','.join(sorted(unknown))}")
        for domain in self.domains.values():
            unknown = set(domain.supported_skills) - set(self.skills)
            if unknown:
                raise ValueError(f"domain_unknown_skills:{domain.id}:{','.join(sorted(unknown))}")


def _index_unique(values: Iterable[Any], key: str) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for value in values:
        identity = str(getattr(value, key))
        if identity in rows:
            raise ValueError(f"duplicate_unified_{key}:{identity}")
        rows[identity] = value
    return rows


def _local_classification(approval: str, side_effect: bool) -> PolicyClass:
    if approval in {"write_only", "ralf_verified"}:
        return PolicyClass.CONFIRM_WRITE
    if side_effect:
        return PolicyClass.PROTECTED
    return PolicyClass.READ


def _side_effect_label(classification: PolicyClass) -> str:
    return {
        PolicyClass.READ: "none",
        PolicyClass.AUTO_WRITE: "reversible_local",
        PolicyClass.CONFIRM_WRITE: "confirmation_required",
        PolicyClass.PROTECTED: "protected",
        PolicyClass.DENY: "denied",
    }[classification]


def _schema_digest(schema: dict[str, Any]) -> str:
    import hashlib

    raw = json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _dedupe_tools(values: Iterable[UnifiedToolSpec]) -> list[UnifiedToolSpec]:
    result: list[UnifiedToolSpec] = []
    seen: set[str] = set()
    for value in values:
        if value.id not in seen:
            result.append(value)
            seen.add(value.id)
    return sorted(result, key=lambda item: item.id)


def _external_skill_domains(name: str) -> tuple[str, ...]:
    if name.startswith("bandi"):
        return ("bandi", "tiremm")
    if name in {"bottazzi_citofono"}:
        return ("home",)
    if name in {"relational_strategy_calculator"}:
        return ("personal_relational",)
    if name in {"tiremm_comms", "runts-contabilita"}:
        return ("tiremm", "email")
    if name in {"grammar"}:
        return ("knowledge", "documents")
    if name in {"programmer_agent"}:
        return ("code",)
    if name in {"stream_doctor", "tv_streaming"}:
        return ("media",)
    if name in {"install_manager", "lab_pc_manager", "atm_telegram"}:
        return ("infrastructure",)
    return ("general_assistant",)


def _external_skill_policy(name: str) -> PolicyClass:
    if name in {
        "atm_telegram", "bandi_browser_fill", "bottazzi_citofono", "install_manager",
        "lab_pc_manager", "runts-contabilita", "tiremm_comms",
    }:
        return PolicyClass.PROTECTED
    if name in {"tv_streaming"}:
        return PolicyClass.AUTO_WRITE
    return PolicyClass.READ


__all__ = ["DEFAULT_HOME_ENTITIES", "DEFAULT_WHATSAPP_SCOPES", "UnifiedRegistryFacade"]
