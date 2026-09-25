from __future__ import annotations

import json
import os
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


def _observable_path_exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


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
        home_provider = os.getenv("RALFLOOP_HOME_PROVIDER", "home_assistant").strip().casefold()
        home_status = (
            "constrained:provider_replaced_by_tuya_mcp"
            if home_provider == "tuya_mcp"
            else "available" if entity_count else "constrained:no_registered_entities"
        )
        tuya_socket = Path(os.getenv("RALF_TUYA_MCP_SOCKET", "/run/ralf-tuya-mcp/mcp.sock"))
        tuya_status = (
            "available" if home_provider == "tuya_mcp" and _observable_path_exists(tuya_socket)
            else "constrained:broker_unavailable" if home_provider == "tuya_mcp"
            else "constrained:not_selected"
        )
        gmail_socket = Path("/run/ralf-google-workspace-mcp/mcp.sock")
        github_socket = Path(os.getenv("RALF_GITHUB_MCP_SOCKET", "/run/ralf-github-mcp/mcp.sock"))
        whatsapp_scopes = json.loads(self.whatsapp_scopes_path.read_text(encoding="utf-8"))
        whatsapp_work_profile = (
            whatsapp_scopes.get("profile") == "work"
            and whatsapp_scopes.get("default_namespace") == "tiremm"
        )
        whatsapp_socket = Path("/run/ralf-whatsapp-mcp/mcp.sock")
        whatsapp_write_enabled = (
            os.getenv("RALFLOOP_WHATSAPP_ASSISTANT_LIVE", "0") == "1"
            and os.getenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "0") == "1"
        )
        mailchimp_socket = Path("/run/ralf-mailchimp-mcp/mcp.sock")
        meteo_socket = Path("/run/ralf-meteo-mcp/mcp.sock")
        editorial_socket = Path("/tmp/ralf-editorial-mcp/mcp.sock")
        bandi_socket = Path(os.getenv("RALF_BANDI_MCP_SOCKET", "/run/ralf-bandi-mcp/mcp.sock"))
        pec_socket = Path(os.getenv("RALF_PEC_MCP_SOCKET", "/run/ralf-pec-mcp/mcp.sock"))
        pec_write_socket = Path(os.getenv("RALF_PEC_WRITE_MCP_SOCKET", "/run/ralf-pec-write-mcp/mcp.sock"))
        arubasign_path = Path(os.getenv("BOTTAZZI_ARUBASIGN_PATH", "/home/bandi/Scaricati/ArubaSign-latest-LINUX/app/lin-x64/ArubaSign"))
        openssl_path = Path(os.getenv("BOTTAZZI_OPENSSL_PATH", "/usr/bin/openssl"))
        browser_socket = Path("/run/ralf-browser-playwright-mcp/mcp.sock")
        rows = [UnifiedToolSpec(
            id="document.sign.arubasign",
            capabilities=("document.sign.prepare", "document.sign.handoff", "document.sign.verify"),
            input_schema="exact local source path + SHA256-bound approval request; secrets excluded",
            output_schema="approval request, user-interaction handoff, or cryptographically verified P7M artifact",
            classification=PolicyClass.CONFIRM_WRITE,
            side_effect_class="approval_bound_user_interactive_signature",
            availability=(
                "available" if _observable_path_exists(arubasign_path) and _observable_path_exists(openssl_path)
                else "constrained:arubasign_or_openssl_unavailable"
            ),
            health="ArubaSign executable + OpenSSL CMS verifier; graphical session checked at handoff",
            verification_method="source hash binding + CMS integrity + extracted-content SHA256 + expected signer; PIN/password/OTP user-only",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "digital_signing.py"),
        ), UnifiedToolSpec(
            id="pec.read.mcp",
            capabilities=(
                "pec_discover_messages",
                "pec_get_message",
                "pec_list_attachments",
                "pec_get_attachment",
                "pec_search_messages",
            ),
            input_schema="strict standalone PEC MCP read-only schemas",
            output_schema="verified PEC messages, bodies and attachment metadata/content",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability="available" if _observable_path_exists(pec_socket) else "constrained:broker_unavailable",
            health="Unix MCP broker + exact five-tool read-only allowlist",
            verification_method="authenticated PEC read; writes=0; sends=0; source provenance",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "pec_mcp_adapter.py"),
        ), UnifiedToolSpec(
            id="pec.write.mcp",
            capabilities=("pec_writer_preflight", "pec_prepare_send", "pec_send_approved"),
            input_schema="strict PEC draft + approval request id; attachment allowlist",
            output_schema="approval-bound draft/send result; delivery receipts verified separately by reader",
            classification=PolicyClass.PROTECTED,
            side_effect_class="approval_bound_external_send",
            availability="available" if _observable_path_exists(pec_write_socket) else "constrained:broker_unavailable",
            health="separate Unix MCP broker; SMTP TLS/auth preflight; exact three-tool allowlist",
            verification_method="hash-bound DomainApprovalStore + one-shot CAS + no retry on uncertain outcome",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "pec_write_mcp_adapter.py"),
        ), UnifiedToolSpec(
            id="bandi.research.mcp",
            capabilities=("bandi_research_now", "bandi_latest", "bandi_search_latest", "bandi_get_opportunity"),
            input_schema="strict Bandi MCP schemas; read-only discovery/review",
            output_schema="ranked opportunities, deadlines, scores, criticalities, citations and persisted report refs",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability="available" if _observable_path_exists(bandi_socket) else "constrained:broker_unavailable",
            health="Unix MCP broker + exact four-tool allowlist",
            verification_method="official-source-first bounded research; persisted report provenance; writes=0; sends=0",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "bandi_mcp_adapter.py"),
        ), UnifiedToolSpec(
            id="editorial.flyer.mcp",
            capabilities=("flyer_create", "flyer_update", "flyer_projects", "flyer_brief", "flyer_review", "flyer_marketing_review", "flyer_media_review", "flyer_render"),
            input_schema="strict editorial MCP schemas; project-bound files only",
            output_schema="brief, marketing score, provenance, HTML, PDF, PNG and preflight",
            classification=PolicyClass.AUTO_WRITE,
            side_effect_class="local_project_filesystem_write",
            availability="available" if _observable_path_exists(editorial_socket) else "constrained:broker_unavailable",
            health="Unix stdio relay + exact eight-tool allowlist",
            verification_method="strict MCP tool discovery; local A4 PDF/PNG/HTML output; no email/print/shell/browser",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "editorial_mcp_adapter.py"),
        ), UnifiedToolSpec(
            id="browser.playwright.read_only",
            capabilities=("browser.snapshot.read", "browser.tabs.read"),
            input_schema="strict browser snapshot or fixed browser_tabs action=list",
            output_schema="untrusted page snapshot/tab metadata as data",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability="available" if _observable_path_exists(browser_socket) else "constrained:broker_unavailable",
            health="shared Playwright MCP Unix bridge",
            verification_method="exact read allowlist; no click/type/upload/evaluate/run_code",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "browser_mcp_adapter.py"),
        ), UnifiedToolSpec(
            id="browser.playwright.approval_bound",
            capabilities=("browser.click", "browser.type", "browser.upload", "browser.submit"),
            input_schema="exact browser action scope bound to approval",
            output_schema="provider result plus post-action readback",
            classification=PolicyClass.CONFIRM_WRITE,
            side_effect_class="confirmation_required",
            availability="constrained:approval_executor_required",
            health="raw Playwright MCP present; automatic execution disabled",
            verification_method="never eligible for READ auto-route; exact approved action required",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "browser_mcp_adapter.py"),
        ), UnifiedToolSpec(
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
            id="github.mcp.read_only",
            capabilities=(
                "github.repo.read",
                "github.issue.list",
                "github.issue.read",
                "github.pr.list",
                "github.pr.read",
            ),
            input_schema="strict GitHub repository/issue/PR identifiers",
            output_schema="GitHub API objects with stable repository and issue/PR ids",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability="available" if _observable_path_exists(github_socket) else "constrained:broker_unavailable",
            health="Unix MCP broker over authenticated gh CLI; Fajoo01 repository allowlist",
            verification_method="read-only gh API; exact tool allowlist; writes=0; sends=0",
            source_registry=str(PROJECT_ROOT / "src" / "github_mcp.py"),
        ), UnifiedToolSpec(
            id="github.mcp.approval_bound",
            capabilities=("github.issue.create", "github.issue.comment"),
            input_schema="exact repository + issue/title/body scope bound to explicit approval",
            output_schema="created issue/comment GitHub object and stable id",
            classification=PolicyClass.CONFIRM_WRITE,
            side_effect_class="confirmation_required",
            availability="constrained:approval_bound_write_broker_required",
            health="read broker refuses mutations by default",
            verification_method="write broker must be enabled only after hash-bound approval; GitHub id readback",
            source_registry=str(PROJECT_ROOT / "src" / "github_mcp.py"),
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
                "available"
                if whatsapp_socket.exists() and whatsapp_work_profile and whatsapp_write_enabled
                else (
                    "constrained:write_feature_disabled"
                    if whatsapp_socket.exists() and whatsapp_work_profile
                    else "constrained:broker_not_started"
                )
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
                "mailchimp.campaign_content.read",
                "mailchimp.members.read",
                "mailchimp.segments.read",
                "mailchimp.tags.read",
                "mailchimp.member_tags.read",
            ),
            input_schema="strict semantic Mailchimp MCP schemas",
            output_schema="verified Mailchimp MCP result contracts",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability=(
                "available"
                if _observable_path_exists(mailchimp_socket)
                else "constrained:broker_unavailable"
            ),
            health="Unix MCP broker + strict tool discovery",
            verification_method="read side_effects=0 + writes=0 + sends=0",
            source_registry=str(PROJECT_ROOT / "src" / "mailchimp.py"),
        ), UnifiedToolSpec(
            id="atm.route.mcp",
            capabilities=(
                "atm.route",
                "atm.route.named",
                "atm.geocode",
            ),
            input_schema="Telegram GPS coordinates or named origin plus destination",
            output_schema="verified ATM route summary / ETA / lines",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability=(
                "available"
                if _observable_path_exists(Path("/run/ralf-atm-mcp/mcp.sock"))
                else "constrained:broker_unavailable"
            ),
            health="Unix MCP broker + strict tool discovery",
            verification_method="read-only ATM routing; side_effects=0",
            source_registry=str(
                PROJECT_ROOT
                / "ralfloop_agent"
                / "unified_assistant"
                / "atm_mcp_adapter.py"
            ),
        ), UnifiedToolSpec(
            id="meteo.radar.mcp",
            capabilities=(
                "meteo.current",
                "meteo.radar",
                "meteo.geocode",
            ),
            input_schema="Telegram GPS coordinates or written address",
            output_schema="verified current weather / forecast / radar URL",
            classification=PolicyClass.READ,
            side_effect_class="none",
            availability=(
                "available"
                if _observable_path_exists(meteo_socket)
                else "constrained:broker_unavailable"
            ),
            health="Unix MCP broker + strict tool discovery",
            verification_method="read-only weather/radar; side_effects=0",
            source_registry=str(
                PROJECT_ROOT
                / "ralfloop_agent"
                / "unified_assistant"
                / "meteo_mcp_adapter.py"
            ),
        ), UnifiedToolSpec(
            id="mailchimp.marketing.approval_bound",
            capabilities=(
                "mailchimp.campaign.create.approved",
                "mailchimp.campaign.send.approved",
                "mailchimp.member.subscribe.approved",
            ),
            input_schema="exact canonical Mailchimp scope + approval_request_id + execution_id",
            output_schema="verified campaign/member state; no generic provider mutation",
            classification=PolicyClass.CONFIRM_WRITE,
            side_effect_class="confirmation_required",
            availability="constrained:approval_and_provider_required",
            health="shared DomainApprovalStore + atomic one-shot claim",
            verification_method="scope digest + Telegram allowlist + provider readback",
            source_registry=str(
                PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "mailchimp_campaign.py"
            ),
        ), UnifiedToolSpec(
            id="tuya.home.mcp",
            capabilities=("home.state.read", "home.service.call"),
            input_schema="strict Tuya MCP schemas over Home Assistant-owned Tuya entities",
            output_schema="Tuya device/entity inventory, state, and verified service readback",
            classification=PolicyClass.PROTECTED,
            side_effect_class="mixed_read_and_policy_gated_write",
            availability=tuya_status,
            health="Unix MCP broker + Tuya ownership verification + Home Assistant readback",
            verification_method="Tuya registry ownership; service allowlist; read before/write/read after",
            source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "tuya_mcp.py"),
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
        rows.extend([
            UnifiedToolSpec(
                id="memory.operational.mcp",
                capabilities=(
                    "memory.documents.search", "memory.timeline.read",
                    "memory.practices.read", "memory.entities.read",
                ),
                input_schema="strict semantic Memory MCP schemas",
                output_schema="provenance-bearing operational memory records",
                classification=PolicyClass.READ,
                side_effect_class="none",
                availability=(
                    "available" if _observable_path_exists(Path("/run/ralf-memory-mcp/mcp.sock"))
                    else "constrained:broker_unavailable"
                ),
                health="Unix MCP broker + SQLite query_only source",
                verification_method="read-only MCP allowlist + source refs/content hashes",
                source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "memory_mcp.py"),
            ),
            UnifiedToolSpec(
                id="arci.read_only.mcp",
                capabilities=(
                    "arci.organization.read", "arci.members.read",
                    "arci.cards.read", "arci.membership.verify",
                ),
                input_schema="strict semantic ARCI MCP schemas",
                output_schema="authenticated ARCI records with read-only semantics",
                classification=PolicyClass.READ, side_effect_class="none",
                availability=(
                    "available" if _observable_path_exists(Path("/run/ralf-arci-mcp/mcp.sock"))
                    else "constrained:broker_unavailable"
                ),
                health="Unix MCP broker + authenticated source",
                verification_method="tool allowlist + provider provenance; side_effects=0",
                source_registry=str(PROJECT_ROOT / "scripts" / "ralf_arci_mcp_server.py"),
            ),
            UnifiedToolSpec(
                id="jellyfin.identity.mcp.read",
                capabilities=(
                    "baffoflix.support.read", "jellyfin.identity.list",
                    "jellyfin.identity.search", "jellyfin.identity.get",
                ),
                input_schema="Jellyfin semantic identity queries and BaffoFlix public support reads",
                output_schema="candidate identities or public BaffoFlix access evidence",
                classification=PolicyClass.READ, side_effect_class="none",
                availability=(
                    "available" if _observable_path_exists(Path("/run/ralf-jellyfin-mcp/mcp.sock"))
                    else "constrained:broker_unavailable"
                ),
                health="Unix MCP broker + Jellyfin provider readback",
                verification_method="read-only discovery + item provenance",
                source_registry=str(PROJECT_ROOT / "scripts" / "ralf_jellyfin_identity_mcp_server.py"),
            ),
            UnifiedToolSpec(
                id="jellyfin.identity.mcp.write",
                capabilities=("jellyfin.identity.apply", "jellyfin.library.refresh", "jellyfin.deduplicate"),
                input_schema="approved Jellyfin identity/library mutation",
                output_schema="provider result requiring post-action verification",
                classification=PolicyClass.PROTECTED, side_effect_class="protected",
                availability="constrained:policy_gate_required",
                health="Unix MCP broker + provider readback",
                verification_method="explicit mutation policy + post-action Jellyfin readback",
                source_registry=str(PROJECT_ROOT / "scripts" / "ralf_jellyfin_identity_mcp_server.py"),
            ),
            UnifiedToolSpec(
                id="teacher.student.mcp",
                capabilities=("teacher.explain", "teacher.exercise", "teacher.quiz", "teacher.progress"),
                input_schema="student-only Teacher MCP schemas",
                output_schema="bounded didactic result",
                classification=PolicyClass.READ, side_effect_class="none",
                availability=(
                    "available" if _observable_path_exists(Path("/run/ralf-teacher-mcp/mcp.sock"))
                    else "constrained:broker_unavailable"
                ),
                health="isolated student MCP broker",
                verification_method="student-only capability boundary; no administrative tools",
                source_registry=str(PROJECT_ROOT / "scripts" / "ralf_teacher_mcp_server.py"),
            ),
            UnifiedToolSpec(
                id="accounting.local.read",
                capabilities=("accounting.inspect",),
                input_schema="Accounting READ objective plus optional verified profile/movements",
                output_schema="Accounting structured artifact with ETS regime, reconciliation and provenance",
                classification=PolicyClass.READ,
                side_effect_class="none",
                availability=(
                    "available"
                    if _observable_path_exists(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "accounting.py")
                    else "constrained:engine_missing"
                ),
                health="local deterministic accounting engine + versioned ETS rulebook",
                verification_method="Decimal arithmetic + explicit missing-input gates + writes=0/sends=0/payments=0/filings=0",
                source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "unified_assistant" / "accounting.py"),
            ),
            UnifiedToolSpec(
                id="visual.memory.local",
                capabilities=("visual_document_retrieval",),
                input_schema="document artifact + query",
                output_schema="page/region evidence with provenance",
                classification=PolicyClass.READ, side_effect_class="none",
                availability="constrained:text_regions_only",
                health="VisualRagWorker present; visual embedding backend not promoted",
                verification_method="source hash + page/region provenance; ColSmol backend pending benchmark",
                source_registry=str(PROJECT_ROOT / "ralfloop_agent" / "local_arch" / "media.py"),
            ),
            UnifiedToolSpec(
                id="amule.books.mcp",
                capabilities=("amule.books.search", "amule.books.download", "amule.queue.read"),
                input_schema="semantic aMule book query/queue request",
                output_schema="search results, queue state or requested download",
                classification=PolicyClass.PROTECTED, side_effect_class="mixed_read_and_write",
                availability=(
                    "available" if _observable_path_exists(Path("/run/ralf-amule-mcp/mcp.sock"))
                    else "constrained:broker_unavailable"
                ),
                health="Unix MCP broker",
                verification_method="search/queue read; downloads subject to workflow policy",
                source_registry="/run/ralf-amule-mcp/mcp.sock",
            ),
        ])
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
