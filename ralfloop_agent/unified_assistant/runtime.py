from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import re
from typing import Any, Mapping

from ralfloop_agent.cli.session_store import SessionStore, SessionStoreError
from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from .contracts import AssistantFeatureFlags, PolicyClass
from .conversation import (
    CONFIRM_WORDS, PENDING_DOMAINS, SessionConversationAdapter, payload_matches,
)
from .core import UnifiedAssistantCore
from .email import EmailWorkingMemoryBuilder
from .email_pipeline import GenericEmailPipeline
from .email_search import GoogleWorkspaceEmailSearch
from .email_send import UnifiedEmailApprovalCoordinator, UnifiedGmailApprovalExecutor
from .mailchimp_campaign import (
    UnifiedMailchimpApprovalCoordinator, UnifiedMailchimpApprovalExecutor,
)
from .jellyfin_identity_write import (
    JellyfinIdentityMCPProvider, UnifiedJellyfinApprovalCoordinator,
    UnifiedJellyfinApprovalExecutor,
)
from .executor import StructuredArtifact, UnifiedDAGExecutor
from .fastweb_portal import FastwebPortalReadOnly
from .home import HomeEntityRegistry, HomeWorkflow
from .home_provider import HomeAssistantProviderError, HomeAssistantRESTBackend
from .tuya_mcp_adapter import TuyaMCPHomeBackend, TuyaMCPProviderError
from .memory import MemoryRouter, tiremm_profile_items
from .atm_mcp_adapter import ATMMCPReadOnly
from .browser_mcp_adapter import (
    BrowserMCPApprovalProvider,
    UnifiedBrowserApprovalCoordinator,
    UnifiedBrowserApprovalExecutor,
    browser_inspect_adapter,
)
from .editorial_mcp_adapter import EditorialMCPContext
from .bandi_mcp_adapter import BandiMCPContext
from .pec_mcp_adapter import PecMCPContext
from .pec_case_support import inspect_difensore_tari_case_status, required_document_gate, stage_tari_supporting_documents
from .pec_write_mcp_adapter import PecWriteMCPContext
from .digital_signing import ArubaSignApprovalWorkflow, DigitalSigningError
from .meteo_mcp_adapter import MeteoMCPReadOnly
from .planner import UnifiedPlanner
from .capability_rag_router import CapabilityRAGRouter
from .recipient import GoogleWorkspaceRecipientResolver
from .registry import DEFAULT_HOME_ENTITIES, UnifiedRegistryFacade
from .skill_adapters import bandi_eligibility_adapter, bandi_read_adapter, research_deep_adapter
from .safe_mcp_read_adapters import (
    arci_context_adapter, education_tutor_adapter, jellyfin_identify_adapter,
    bandi_discovery_adapter, knowledge_retrieve_adapter, runts_context_adapter,
)
from .accounting import accounting_read_adapter
from .whatsapp_compose import EmailBackedWhatsAppDraftPipeline, UnifiedWhatsAppComposeService
from .whatsapp_mcp_adapter import WhatsAppMCPReadOnly
from .whatsapp_send import (
    UnifiedWhatsAppApprovalCoordinator, UnifiedWhatsAppApprovalExecutor,
)
from .runts_write import (
    RUNTS_REPLY_ACTION,
    RuntsApprovedReplyExecutor,
    UnifiedRuntsApprovalCoordinator,
    build_runts_pending_payload,
)
from .runts_browser_adapter import (
    RuntsAuthenticatedBrowserAdapter,
    RuntsAuthenticatedCdpTransport,
)
from .runts_browser_write import (
    RuntsAuthenticatedCdpWriteTransport,
)
from src.mailchimp import MailchimpApprovedMCPWorkflow, MailchimpMCPContext
from src.whatsapp import WhatsAppMCPContext


_LEGACY = re.compile(r"^(?:/|rl:|rsc\b|abc\b|atm\b|apri\s+cancello\b)", re.I)
_SUPPORTED = re.compile(
    r"\b(?:scrivi\s+(?:una\s+mail\s+)?a|prepara\s+(?:una\s+)?(?:mail|email)|"
    r"manda\s+(?:una\s+)?(?:mail|email)|rispond(?:i|ere)\s+(?:all['’]\s*|alla\s+|a\s+questa\s+)(?:mail|email|appello|comunicazione)|"
    r"rispondi\s+(?:a|alla\s+mail(?:\s+di)?)|accendi|spegni|apri|chiudi|"
    r"imposta|metti|porta|abbassala|alzala|temperatura|quanto\s+fa|fa\s+caldo|"
    r"fa\s+freddo|rendila|cambiala|aggiungi|modifica|ok|invia|mandala|va\s+bene|annulla|"
    r"fastweb|myfastpage|whatsapp|wapp|mailchimp|pec|posta\s+certificata|posta\s+elettronica\s+certificata|webmail\s+pec|difensore\s+(?:civico|regionale)|"
    r"meteo|weather|previsioni|piove|piover[aà]|pioggia|"
    r"temporale|radar|precipitazioni|vento|atm|giromilano|"
    r"mezzi\s+pubblici|trasporto\s+pubblico|portami|"
    r"(?:devo|voglio|vorrei)\s+(?:andare|arrivare)(?:\s+(?:da|dal|dalla|dallo|dai|dagli|dalle|a|ad|al|alla|allo|ai|agli|alle|all['’]|in)\b|\s*$)|"
    r"band[oi]|grant|contribut[oi]|finanziament[oi]|candidatur[ae]|opportunit[aà]|"
    r"volantin[oi]|flyer|locandin[ae]|manifest[oi]|poster|"
    r"come\s+(?:arrivo|vado|posso\s+andare)|"
    r"mezzi\s+(?:per|verso)|percorso\s+(?:atm|con\s+i\s+mezzi)|home\s+assistant|domotica|stato\s+(?:della\s+)?luce|"
    r"runts|arci|jellyfin|browser|playwright|snapshot\s+(?:browser|pagina)|schede?\s+browser|bandi|bando|grant|finanziament[oi]|contribut[oi]|insegnante|tutor|quiz|esercizio\s+didattico|memoria\s+operativa|firma|firmare|digitalmente|arubasign|p7m|"
    r"commercialista|contabilit[aà]|bilancio\s+ets|rendiconto(?:\s+ets|\s+per\s+cassa)?|prima\s+nota|riconciliazion[ei]|fattur[ae]|ricevut[ae]|f24|iva|scadenz[ae]\s+fiscal[ei])\b",
    re.I,
)

_EMAIL_READ_SUPPORTED = re.compile(
    r"\b(?:controlla|cerca|trova|verifica|guarda|leggi|abbiamo\s+ricevuto|ha\s+mai|ci\s+ha|ci\s+aveva)\b.*"
    r"\b(?:mail|email|posta|bozz[ae]|scritto|comunicat[oaie]|comunicazioni|avvisat[oaie]|messaggi?|thread)\b",
    re.I,
)

_ASSISTANT_V1_EXTENDED_SUPPORTED = re.compile(
    r"\b(?:ricerca|document[oi]|pdf|allegat[oi]|repository|repo|codice|debug|refactor|"
    r"server|servizi?|spazio\s+disco|tiremm)\b",
    re.I,
)
_ASSISTANT_V1_GROUNDED_ADMIN_TOPIC = re.compile(
    r"\b(?:aps|ets|runts|terzo\s+settore|codice\s+del\s+terzo\s+settore|"
    r"associazion[ei]\s+di\s+promozione\s+sociale|enti?\s+del\s+terzo\s+settore)\b",
    re.I,
)
_ASSISTANT_V1_GROUNDED_ADMIN_QUERY = re.compile(
    r"\b(?:cos['’]?[eè]|che\s+cos['’]?[eè]|definisci|spiega|normativ[ae]|legge|"
    r"decreto|d\.?\s*lgs\.?|articol[oi]|requisit[oi]|obbligh[oi]|disciplina|"
    r"cosa\s+prevede|chi\s+pu[oò]|come\s+funziona)\b",
    re.I,
)


def _assistant_v1_extended_request(text: str, context: Mapping[str, Any]) -> bool:
    return (
        str(context.get("assistant_surface") or "") == "assistant_v1"
        and bool(_ASSISTANT_V1_EXTENDED_SUPPORTED.search(text))
    )


def _assistant_v1_grounded_admin_request(text: str, context: Mapping[str, Any]) -> bool:
    return (
        str(context.get("assistant_surface") or "") == "assistant_v1"
        and bool(_ASSISTANT_V1_GROUNDED_ADMIN_TOPIC.search(text))
        and bool(_ASSISTANT_V1_GROUNDED_ADMIN_QUERY.search(text))
    )


_PEC_SOURCE_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,63}\b", re.I)
_PEC_PROTOCOL_RE = re.compile(r"\bprotocollo(?:\s+numero)?\s+([A-Z0-9.\-_/]+)", re.I)


def _pec_existing_draft(protocol: str) -> str:
    if not protocol or re.fullmatch(r"[A-Z0-9.-]+", protocol, re.I) is None:
        return ""
    root = Path(os.getenv("BOTTAZZI_PEC_OUTBOX_ROOT", "/var/lib/ralfloop/pec-outbox")).resolve()
    path = root / f"DRAFT_reply_{protocol}.txt"
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        return ""


def _pec_prepare_values(
    arguments: Mapping[str, Any],
    inputs: Mapping[str, Any],
    objective: str,
) -> dict[str, Any]:
    """Fill missing PEC draft fields only from the upstream read artifact."""
    values = dict(arguments)
    source = inputs.get("artifact.pec_source")
    if not isinstance(source, Mapping):
        return values
    payload = source.get("payload")
    if not isinstance(payload, Mapping):
        return values
    messages = payload.get("messages") or ()
    first = next((item for item in messages if isinstance(item, Mapping)), None)
    if first is None:
        return values

    sender = str(first.get("sender") or "")
    source_subject = " ".join(str(first.get("subject") or "").split())
    source_body = str(first.get("body") or "")

    if not str(values.get("recipient") or "").strip():
        addresses = [match.group(0) for match in _PEC_SOURCE_EMAIL_RE.finditer(sender)]
        recipient = next(
            (item for item in addresses if item.casefold() != "tiremminnanz@pec.it"),
            "",
        )
        if recipient:
            values["recipient"] = recipient

    if not str(values.get("subject") or "").strip() and source_subject:
        clean_subject = re.sub(r"^POSTA\s+CERTIFICATA:\s*", "", source_subject, flags=re.I)
        values["subject"] = f"Riscontro: {clean_subject or source_subject}"

    if not str(values.get("body") or "").strip() and source_subject:
        protocol_match = _PEC_PROTOCOL_RE.search(source_body)
        protocol = protocol_match.group(1) if protocol_match else ""
        existing_draft = _pec_existing_draft(protocol) if "difensore" in objective.casefold() else ""
        if existing_draft:
            values["body"] = existing_draft
        else:
            practice = "pratica TARI" if "tari" in objective.casefold() else "pratica indicata"
            protocol_text = f" (protocollo {protocol})" if protocol else ""
            values["body"] = (
                "Spett.le Ufficio,\n\n"
                f"in riscontro alla Vostra PEC «{source_subject}»{protocol_text}, "
                f"inviamo il presente riscontro relativo alla {practice}.\n\n"
                "Cordiali saluti"
            )
    return values


def is_unified_telegram_request(text: str, context: Mapping[str, Any]) -> bool:
    flags = AssistantFeatureFlags.from_env()
    source = str(context.get("source") or "")
    explicit_runts = _is_explicit_runts_approval(text, context)
    return (
        flags.unified_assistant
        and (source.startswith("telegram_") or source == "ralf_terminal")
        and (explicit_runts or not _LEGACY.search(text.strip()))
        and bool(
            _SUPPORTED.search(text)
            or _EMAIL_READ_SUPPORTED.search(text)
            or _is_pec_runts_request(text)
            or explicit_runts
            or re.fullmatch(r"\s*(?:otp[\s:-]*)?[0-9]{6}\s*", text, re.I)
            or (
                _is_positive_confirmation(text)
                and _has_single_approvable_pending(context)
            )
        )
    )


def _build_unified_planner(registry: UnifiedRegistryFacade) -> UnifiedPlanner:
    """Canonical Telegram/Ralf planner; filtered runtimes inject their own policy."""
    return UnifiedPlanner(registry, capability_router=CapabilityRAGRouter(registry))


def _has_single_approvable_pending(context: Mapping[str, Any]) -> bool:
    """Route bare confirmations only for one existing, hash-valid pending."""

    try:
        session_id = _session_id(context)
        store = SessionStore(os.getenv(
            "RALFLOOP_UNIFIED_SESSION_DIR",
            str(Path.home() / ".local" / "state" / "ralf" / "unified-sessions"),
        ))
        conversation = SessionConversationAdapter(store).load(session_id)
    except (OSError, SessionStoreError, TypeError, ValueError):
        return False
    active = [
        item for name in PENDING_DOMAINS
        if (item := getattr(conversation.state.pending, name)) is not None
        and item.expires_at > int(datetime.now(UTC).timestamp())
    ]
    return (
        len(active) == 1
        and active[0].domain in {"email", "whatsapp", "mailchimp", "jellyfin"}
        and active[0].policy.value in {"CONFIRM_WRITE", "PROTECTED"}
        and bool(active[0].approval_ref)
        and payload_matches(active[0])
    )


def unified_route_probe(
    text: str,
    context: Mapping[str, Any],
    *,
    flags_override: AssistantFeatureFlags | None = None,
) -> dict[str, Any] | None:
    """Side-effect-free route metadata for Meowgram and Assistant v1."""

    flags = flags_override or AssistantFeatureFlags.from_env()
    source = str(context.get("source") or "")
    if not (
        flags.unified_assistant
        and (source.startswith("telegram_") or source == "ralf_terminal")
    ):
        return None
    if not is_unified_telegram_request(text, context) and not flags_override:
        return None
    if flags_override and not (
        _SUPPORTED.search(text)
        or _EMAIL_READ_SUPPORTED.search(text)
        or _assistant_v1_extended_request(text, context)
        or _assistant_v1_grounded_admin_request(text, context)
        or _is_pec_runts_request(text)
        or _is_explicit_runts_approval(text, context)
        or re.fullmatch(r"\s*(?:otp[\s:-]*)?[0-9]{6}\s*", text, re.I)
        or (_is_positive_confirmation(text) and _has_single_approvable_pending(context))
    ):
        return None
    if _is_explicit_runts_approval(text, context):
        practice_id = _RUNTS_EXPLICIT_APPROVAL.fullmatch(text).group("practice")
        return {
            "task_mode": "external_action", "mode": "external_action",
            "interaction_class": "EXTERNAL_ACTION",
            "intent": "runts.practice.reply.approve",
            "arguments": {"practice_id": practice_id}, "domains": ["runts"],
            "skills_used": ["runts.practice.reply.approve"],
            "domain_skills": ["runts.practice.reply.approve"],
            "mcp_used": [], "mcp_connectors": [],
            "write_policy": "policy_gated", "evidence_first": True,
            "requires_confirmation": False,
        }
    if _is_pec_runts_request(text):
        from .pec_runts_telegram import decision_for_telegram

        decision = decision_for_telegram(text)
        assert decision is not None
        if decision.tool_id=="runts_prepare_practice_response":
            return {"task_mode":"tool_backed_prepare","mode":"tool_backed_prepare","interaction_class":"TOOL_BACKED_PREPARE",
                "intent":"runts.practice.response.prepare","arguments":decision.arguments.model_dump(),"domains":["pec_runts"],
                "skills_used":[decision.tool_id],"domain_skills":[decision.tool_id],"mcp_used":["pec_runts.mcp"],
                "mcp_connectors":["pec_runts.mcp"],"write_policy":"no_write","evidence_first":True,"requires_confirmation":False}
        pec_intent = (
            "pec.inbox.read"
            if decision.tool_id == "pec_discover_messages"
            else "pec.runts_reference.read"
        )
        return {
            "task_mode": "tool_backed_read", "mode": "tool_backed_read",
            "interaction_class": "TOOL_BACKED_READ", "intent": pec_intent,
            "arguments": decision.arguments.model_dump(), "domains": ["pec_runts"],
            "skills_used": [decision.tool_id], "domain_skills": [decision.tool_id],
            "mcp_used": ["pec_runts.mcp"], "mcp_connectors": ["pec_runts.mcp"],
            "write_policy": "no_write", "evidence_first": True,
            "requires_confirmation": False,
        }
    if re.fullmatch(r"\s*(?:otp[\s:-]*)?[0-9]{6}\s*", text, re.I):
        return {
            "task_mode": "external_action", "mode": "external_action",
            "interaction_class": "EXTERNAL_ACTION", "intent": "email.otp",
            "arguments": {}, "domains": ["email"],
            "skills_used": ["email.approve"], "domain_skills": ["email.approve"],
            "mcp_used": [], "mcp_connectors": [], "write_policy": "policy_gated",
            "evidence_first": True, "requires_confirmation": True,
        }
    registry = UnifiedRegistryFacade()
    planner = _build_unified_planner(registry)
    plan = planner.validate(planner.plan(text))
    skills = [item.skill for item in plan.assignments]
    all_read = all(item.policy.value == "READ" for item in plan.assignments)
    task_mode = "tool_backed_read" if all_read else "external_action"
    interaction_class = "TOOL_BACKED_READ" if all_read else "EXTERNAL_ACTION"
    connectors = []
    if "email.search" in skills or (not all_read and any(item.domain == "email" for item in plan.assignments)):
        connectors.append("google_workspace.gmail")
    if "fastweb.portal.read" in skills:
        connectors.append("fastweb.portal.read_only")
    if "whatsapp.read" in skills or (not all_read and any(item.domain == "whatsapp" for item in plan.assignments)):
        connectors.append("whatsapp.web.mcp")
    if "mailchimp.read" in skills or (not all_read and any(item.domain == "mailchimp" for item in plan.assignments)):
        connectors.append("mailchimp.marketing")
    if "meteo.read" in skills:
        connectors.append("meteo.radar.mcp")
    if "atm.route" in skills:
        connectors.append("atm.route.mcp")
    if "pec.read" in skills:
        connectors.append("pec.read.mcp")
    if "pec.prepare_send" in skills or "pec.send_approved" in skills:
        connectors.append("pec.write.mcp")
    if "documents.sign" in skills:
        connectors.append("document.sign.arubasign")
    if any(skill.startswith("bandi.") for skill in skills):
        connectors.append("bandi.research.mcp")
    if "research.deep" in skills:
        connectors.append("model_tool.deep_web_research")
    if any(skill in {"knowledge.retrieve", "runts.context"} for skill in skills):
        connectors.append("memory.operational.mcp")
    if "arci.context" in skills:
        connectors.append("arci.read_only.mcp")
    if "jellyfin.identify" in skills:
        connectors.append("jellyfin.identity.mcp.read")
    if "education.tutor" in skills:
        connectors.append("teacher.student.mcp")
    if "browser.inspect" in skills:
        connectors.append("browser.playwright.read_only")
    if "accounting.read" in skills:
        connectors.append("accounting.local.read")
    if any(skill in {"home.read", "home.control"} for skill in skills):
        home_provider = os.getenv("RALFLOOP_HOME_PROVIDER", "home_assistant").strip().casefold()
        connectors.append("tuya.home.mcp" if home_provider == "tuya_mcp" else "home_assistant.adapter")
    if not all_read and any(item.domain == "jellyfin" for item in plan.assignments):
        connectors.append("jellyfin.identity.mcp.write")
    if not all_read and any(item.domain == "browser" for item in plan.assignments):
        connectors.append("browser.playwright.approval_bound")
    return {
        "task_mode": task_mode,
        "mode": task_mode,
        "interaction_class": interaction_class,
        "intent": plan.intent,
        "arguments": dict(plan.assignments[0].arguments),
        "domains": list(plan.domains),
        "skills_used": skills,
        "domain_skills": skills,
        "mcp_used": connectors,
        "mcp_connectors": connectors,
        "write_policy": "no_write" if task_mode == "tool_backed_read" else "policy_gated",
        "evidence_first": True,
        "requires_confirmation": any(
            item.policy.value in {"CONFIRM_WRITE", "PROTECTED"} for item in plan.assignments
        ),
    }


def run_unified_telegram(
    text: str,
    context: Mapping[str, Any],
    *,
    flags_override: AssistantFeatureFlags | None = None,
) -> dict[str, Any]:
    if _is_explicit_runts_approval(text, context):
        return _execute_explicit_runts_approval(
            text,
            context,
        )

    if _is_pec_runts_request(text):
        from .pec_runts_telegram import execute_telegram_read

        memory_path = Path(os.getenv(
            "RALFLOOP_OPERATIONAL_MEMORY_PATH",
            str(Path.home() / ".local" / "state" / "ralf" / "operational-memory.sqlite"),
        ))

        result = execute_telegram_read(
            text,
            memory_path=memory_path,
        )

        if (
            result.get("capability")
            == "runts_prepare_practice_response"
            and result.get("ok")
            and isinstance(
                result.get("metadata", {}).get(
                    "proposal"
                ),
                Mapping,
            )
        ):
            return _stage_runts_prepare_for_approval(
                result,
                context,
            )

        return result

    flags = flags_override or AssistantFeatureFlags.from_env()
    session_id = _session_id(context)
    store = SessionStore(os.getenv(
        "RALFLOOP_UNIFIED_SESSION_DIR",
        str(Path.home() / ".local" / "state" / "ralf" / "unified-sessions"),
    ))
    _ensure_session(store, session_id)
    session_adapter = SessionConversationAdapter(store)
    conversation = session_adapter.load(session_id)
    registry = UnifiedRegistryFacade()
    profile = Path(__file__).resolve().parents[2] / "config" / "reply_context_profiles.json"
    memory_items = tiremm_profile_items(profile) if profile.exists() else ()
    memory = MemoryRouter(memory_items)
    fastweb_portal = FastwebPortalReadOnly.from_environment()
    whatsapp_gateway_factory = WhatsAppMCPContext.from_environment
    whatsapp_read = WhatsAppMCPReadOnly(whatsapp_gateway_factory)
    mailchimp_gateway_factory = MailchimpMCPContext.from_environment
    meteo_read = MeteoMCPReadOnly(context)
    atm_read = ATMMCPReadOnly(context)
    editorial_gateway_factory = EditorialMCPContext.from_environment
    bandi_gateway_factory = BandiMCPContext.from_environment
    pec_gateway_factory = PecMCPContext.from_environment
    pec_write_gateway_factory = PecWriteMCPContext.from_environment
    signing_policy = DomainApprovalPolicy.from_env()
    signing_store = DomainApprovalStore(policy=signing_policy)
    signing_workflow = ArubaSignApprovalWorkflow.from_environment(signing_store)
    home_workflow = None
    if flags.home_assistant_read_live or flags.home_assistant_live:
        try:
            provider = os.getenv("RALFLOOP_HOME_PROVIDER", "home_assistant").strip().casefold()
            backend = TuyaMCPHomeBackend.from_environment() if provider == "tuya_mcp" else HomeAssistantRESTBackend.from_environment()
            home_workflow = HomeWorkflow(HomeEntityRegistry.load(DEFAULT_HOME_ENTITIES), backend)
        except (OSError, ValueError, HomeAssistantProviderError, TuyaMCPProviderError):
            home_workflow = None
    approval_coordinator = None
    approval_executor = None
    whatsapp_approval_coordinator = None
    whatsapp_approval_executor = None
    mailchimp_approval_coordinator = None
    mailchimp_approval_executor = None
    jellyfin_approval_coordinator = None
    jellyfin_approval_executor = None
    browser_approval_coordinator = None
    browser_approval_executor = None
    browser_interaction_provider = (
        BrowserMCPApprovalProvider() if flags.browser_interact_live else None
    )
    jellyfin_identity_provider = (
        JellyfinIdentityMCPProvider() if flags.jellyfin_identity_write_live else None
    )
    if (
        flags.email_assistant_live or flags.whatsapp_assistant_live
        or flags.mailchimp_campaign_live or flags.jellyfin_identity_write_live
        or flags.browser_interact_live
    ):
        policy = DomainApprovalPolicy.from_env()
        if policy.enabled:
            approval_store = DomainApprovalStore(policy=policy)
            if flags.email_assistant_live:
                account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
                approval_coordinator = UnifiedEmailApprovalCoordinator(
                    approval_store, policy=policy, account=account,
                    outbox_path=os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX") or None,
                )
                approval_executor = UnifiedGmailApprovalExecutor.from_environment(
                    store=approval_store
                )
            if flags.whatsapp_assistant_live:
                whatsapp_approval_coordinator = UnifiedWhatsAppApprovalCoordinator(
                    approval_store, policy=policy,
                )
                whatsapp_approval_executor = UnifiedWhatsAppApprovalExecutor.from_environment(
                    store=approval_store,
                )
            if flags.mailchimp_campaign_live:
                mailchimp_approval_coordinator = UnifiedMailchimpApprovalCoordinator(
                    approval_store, policy=policy,
                )
                mailchimp_approval_executor = UnifiedMailchimpApprovalExecutor(
                    lambda: MailchimpApprovedMCPWorkflow(),
                )
            if flags.jellyfin_identity_write_live and jellyfin_identity_provider is not None:
                jellyfin_approval_coordinator = UnifiedJellyfinApprovalCoordinator(
                    approval_store, policy=policy,
                )
                jellyfin_approval_executor = UnifiedJellyfinApprovalExecutor(
                    approval_store, jellyfin_identity_provider, write_enabled=True,
                )
            if flags.browser_interact_live and browser_interaction_provider is not None:
                browser_approval_coordinator = UnifiedBrowserApprovalCoordinator(
                    approval_store, policy=policy,
                )
                browser_approval_executor = UnifiedBrowserApprovalExecutor(
                    approval_store, browser_interaction_provider, write_enabled=True,
                )
    previous_email = conversation.state.pending.email
    previous_whatsapp = conversation.state.pending.whatsapp
    previous_mailchimp = conversation.state.pending.mailchimp
    previous_jellyfin = conversation.state.pending.jellyfin
    previous_browser = conversation.state.pending.browser
    approval_transition: dict[str, Any] = {}
    if any((approval_coordinator, whatsapp_approval_coordinator, mailchimp_approval_coordinator, jellyfin_approval_coordinator, browser_approval_coordinator)) and _is_positive_confirmation(text):
        active = [
            item for name in PENDING_DOMAINS
            if (item := getattr(conversation.state.pending, name)) is not None
        ]
        if len(active) == 1 and active[0].domain in {"email", "whatsapp", "mailchimp", "jellyfin", "browser"}:
            pending = active[0]
            coordinator = ({
                "email": approval_coordinator,
                "whatsapp": whatsapp_approval_coordinator,
                "mailchimp": mailchimp_approval_coordinator,
                "jellyfin": jellyfin_approval_coordinator,
                "browser": browser_approval_coordinator,
            })[pending.domain]
            if coordinator is not None:
                approval_transition = coordinator.approve(
                pending,
                telegram_user_id=int(context.get("telegram_user_id") or 0),
                telegram_chat_id=int(context.get("telegram_chat_id") or 0),
                telegram_message_id=int(context.get("telegram_message_id") or 0),
                chat_type=str(context.get("telegram_chat_type") or "private"),
            )
            if approval_transition.get("status") in {"approved", "already_approved"}:
                conversation.bind_approval(
                    domain=pending.domain,
                    pending_id=pending.pending_id,
                    payload_digest=pending.payload_digest,
                    approval_ref=str(pending.approval_ref),
                )
    email_memory_builder = EmailWorkingMemoryBuilder(memory, registry.domain("email"))
    email_pipeline = GenericEmailPipeline()
    whatsapp_compose = UnifiedWhatsAppComposeService(
        whatsapp_gateway_factory,
        draft_pipeline=EmailBackedWhatsAppDraftPipeline(
            email_memory_builder, email_pipeline,
        ),
    )
    core = UnifiedAssistantCore(
        planner=_build_unified_planner(registry),
        conversation=conversation,
        flags=flags,
        email_memory=email_memory_builder,
        email_pipeline=email_pipeline,
        email_search=GoogleWorkspaceEmailSearch.from_environment(),
        fastweb_portal=fastweb_portal,
        whatsapp_read=whatsapp_read,
        mailchimp_gateway_factory=mailchimp_gateway_factory,
        meteo_read=meteo_read,
        atm_read=atm_read,
        mailchimp_campaign_artifact_provider=lambda skill: (
            context.get("mailchimp_campaign_draft")
            if skill == "mailchimp.campaign.create"
            else context.get("mailchimp_campaign_verified")
        ),
        whatsapp_compose=whatsapp_compose,
        recipient_resolver=GoogleWorkspaceRecipientResolver.from_environment(),
        approval_executor=approval_executor,
        whatsapp_approval_executor=whatsapp_approval_executor,
        mailchimp_approval_executor=mailchimp_approval_executor,
        jellyfin_identity_provider=jellyfin_identity_provider,
        jellyfin_approval_executor=jellyfin_approval_executor,
        browser_interaction_provider=browser_interaction_provider,
        browser_approval_executor=browser_approval_executor,
        home_workflow=home_workflow,
        memory_router=memory,
    )
    otp_result = None
    otp_match = re.fullmatch(r"\s*(?:otp[\s:-]*)?([0-9]{6})\s*", text, re.I)
    if otp_match:
        pending = conversation.state.pending.email
        chat_id = int(context.get("telegram_chat_id") or 0)
        if (
            pending is None or not pending.approval_ref
            or approval_coordinator is None or approval_executor is None
            or approval_executor.otp_gate is None
            or chat_id not in approval_coordinator.policy.allowed_chat_ids
        ):
            otp_result = core._result(
                "email_otp_denied", "OTP non associato a una bozza email approvata."
            )
        else:
            approval_row = approval_coordinator.store.get_request(pending.approval_ref)
            if not approval_row or approval_row.get("status") != "approved":
                otp_result = core._result(
                    "email_otp_denied", "Prima approva la bozza email mostrata."
                )
            else:
                verified = approval_executor.otp_gate.submit_telegram_reply(
                    approval_row,
                    otp=otp_match.group(1),
                    telegram_chat_id=chat_id,
                    telegram_message_id=int(context.get("telegram_message_id") or 0),
                )
                if verified.get("approved"):
                    conversation.bind_approval(
                        domain="email", pending_id=pending.pending_id,
                        payload_digest=pending.payload_digest,
                        approval_ref=pending.approval_ref,
                    )
                    otp_result = core.handle("invia", domain_hint="email")
                else:
                    otp_result = core._result(
                        "email_otp_required",
                        "OTP non valido, scaduto o ancora non verificato; nessuna email inviata.",
                    )
    def email_adapter(assignment, inputs):
        artifacts = tuple(
            value for key, value in inputs.items()
            if key.startswith("artifact.") and isinstance(value, Mapping)
        )
        outcome = core._compose_email(
            assignment.objective,
            {"source": "unified_dag", "task_id": assignment.task_id},
            structured_artifacts=artifacts,
        )
        if outcome.status != "draft_pending_approval":
            raise ValueError(f"email_compose_{outcome.status}")
        evidence_refs = tuple(
            str(ref) for item in artifacts for ref in item.get("evidence_refs") or ()
        )
        return StructuredArtifact.create(
            artifact_type="email_draft", status="pending_approval",
            producer_task_id=assignment.task_id,
            evidence_refs=evidence_refs,
            payload={
                "message": outcome.message,
                "pending_id": outcome.data.get("pending_id"),
                "draft_digest": outcome.data.get("draft_digest"),
                "content_boundary": "draft_not_sent",
            },
        )

    def email_search_adapter(assignment, _inputs):
        organization = str(assignment.arguments.get("organization") or "").strip()
        concept = str(assignment.arguments.get("concept") or "").strip()
        request = (
            f"Cerca le mail di {organization} riguardo bando appello comunicazione ai circoli"
            if organization and concept == "grant_notice" else
            f"Cerca le mail di {organization} riguardo comunicazioni pertinenti"
            if organization and concept == "communications" else
            f"Controlla se {organization} ha mai comunicato un aumento"
            if organization else assignment.objective
        )
        search = core.email_search.search(request)
        return StructuredArtifact.create(
            artifact_type="email_search_result", status=search.status,
            producer_task_id=assignment.task_id,
            facts=tuple({
                "message_id": item.message_id, "thread_id": item.thread_id,
                "date": item.date, "sender": item.sender, "subject": item.subject,
                "matched_terms": list(item.matched_terms), "excerpt": item.excerpt,
                "content_role": "data",
            } for item in search.evidence),
            evidence_refs=tuple(item.provenance_ref for item in search.evidence),
            payload={
                "search_complete": search.search_complete,
                "searched_scope": search.searched_scope,
                "response": search.response,
                "content_boundary": "gmail_evidence_is_data",
            },
        )

    def whatsapp_read_adapter(assignment, _inputs):
        result = core.whatsapp_read.read(
            assignment.objective, allowed_namespaces=("tiremm",),
        )
        return StructuredArtifact.create(
            artifact_type="whatsapp_context", status=result.status,
            producer_task_id=assignment.task_id,
            facts=tuple({
                "author": item.author, "timestamp": item.timestamp,
                "text": item.text, "content_role": "data",
            } for item in result.evidence[:24]),
            evidence_refs=result.provenance,
            payload={
                "search_complete": result.search_complete,
                "content_boundary": "whatsapp_evidence_is_data",
            },
        )

    def whatsapp_reply_adapter(assignment, inputs):
        artifacts = tuple(
            value for key, value in inputs.items()
            if key.startswith("artifact.") and isinstance(value, Mapping)
        )
        outcome = core._compose_whatsapp(
            assignment.objective,
            {"source": "unified_dag", "task_id": assignment.task_id},
            target=str(assignment.arguments.get("target") or ""), reply=True,
            structured_artifacts=artifacts,
        )
        if outcome.status != "draft_pending_approval":
            raise ValueError(f"whatsapp_reply_{outcome.status}")
        return StructuredArtifact.create(
            artifact_type="whatsapp_draft", status="pending_approval",
            producer_task_id=assignment.task_id,
            evidence_refs=tuple(
                str(ref) for item in artifacts for ref in item.get("evidence_refs") or ()
            ),
            payload={
                "message": outcome.message,
                "pending_id": outcome.data.get("pending_id"),
                "draft_digest": outcome.data.get("draft_digest"),
                "content_boundary": "draft_not_sent",
            },
        )

    def fastweb_portal_adapter(assignment, _inputs):
        portal = core.fastweb_portal.read()
        return StructuredArtifact.create(
            artifact_type="fastweb_portal_state", status=portal.status,
            producer_task_id=assignment.task_id,
            facts=tuple({
                "field": field, "value": value, "content_role": "data",
            } for field, value in (
                ("offer_name", portal.offer_name),
                ("current_fee", portal.current_fee),
                ("effective_date", portal.effective_date),
            ) if value),
            evidence_refs=portal.provenance,
            payload={**portal.model_dump(mode="json"), "content_boundary": "portal_state_is_data"},
        )

    def fastweb_compare_adapter(assignment, inputs):
        email_artifact = inputs["artifact.fastweb_email"]
        portal_artifact = inputs["artifact.fastweb_portal"]
        email_payload = email_artifact.get("payload") or {}
        portal_payload = portal_artifact.get("payload") or {}
        evidence_count = len(email_artifact.get("facts") or ())
        fee = portal_payload.get("current_fee")
        offer = portal_payload.get("offer_name")
        message = (
            f"MyFastPage: offerta {offer or 'non trovata'}, canone corrente "
            f"{fee or 'non esposto esplicitamente'}. Gmail: {evidence_count} comunicazioni pertinenti; "
            f"completezza ricerca={bool(email_payload.get('search_complete'))}. "
            "Le fonti restano separate; nessuna corrispondenza contrattuale è inferita automaticamente."
        )
        refs = tuple(dict.fromkeys(
            tuple(email_artifact.get("evidence_refs") or ())
            + tuple(portal_artifact.get("evidence_refs") or ())
        ))
        return StructuredArtifact.create(
            artifact_type="fastweb_comparison", status="completed",
            producer_task_id=assignment.task_id, evidence_refs=refs,
            payload={
                "message": message,
                "gmail_status": email_artifact.get("status"),
                "portal_status": portal_artifact.get("status"),
                "content_boundary": "source_artifacts_are_data",
            },
        )

    def pec_adapter(assignment, _inputs):
        with pec_gateway_factory() as gateway:
            result = gateway.request(assignment.objective)
        folded_objective = assignment.objective.casefold()
        status_question = (
            "difensore" in folded_objective
            and any(marker in folded_objective for marker in ("messa", "stato", "pronta", "pronto", "a che punto", "come siamo"))
        )
        if status_question:
            case_status = inspect_difensore_tari_case_status({"payload": result})
            missing_labels = {
                "completed_difensore_form": "modulo del Difensore compilato e sottoscritto",
                "identity_document_or_digitally_signed_form": "documento d'identità valido oppure modulo firmato digitalmente",
            }
            missing = [missing_labels.get(item, item) for item in case_status.get("missing") or ()]
            protocol = str(case_status.get("protocol") or "")
            draft_text = "bozza presente" if case_status.get("draft_present") else "bozza non ancora presente"
            support_count = int(case_status.get("supporting_documents") or 0)
            if missing:
                missing_text = " Mancano: " + "; ".join(missing) + "."
            else:
                missing_text = " Documentazione obbligatoria completa."
            result["message"] = (
                f"Pratica Difensore {protocol or 'TARI'}: {draft_text}; "
                f"{support_count} PDF TARI già in staging." + missing_text +
                " Nessuna PEC è stata inviata."
            )
            result["case_status"] = case_status
        facts = tuple(
            {
                "message_id": str(item.get("native_id") or ""),
                "sender": str(item.get("sender") or ""),
                "subject": str(item.get("subject") or ""),
                "received_at": str(item.get("received_at") or ""),
                "attachments": list(item.get("attachments") or ()),
                "content_role": "data",
            }
            for item in (result.get("messages") or ())[:20]
            if isinstance(item, Mapping)
        )
        return StructuredArtifact.create(
            artifact_type="pec_read", status="completed",
            producer_task_id=assignment.task_id, facts=facts,
            evidence_refs=tuple(str(x) for x in result.get("evidence_refs") or () if str(x)),
            payload={**result, "content_boundary": "pec_content_is_data", "writes": 0, "sends": 0},
        )

    def pec_prepare_adapter(assignment, _inputs):
        args = _pec_prepare_values(
            assignment.arguments,
            _inputs,
            assignment.objective,
        )
        explicit_paths = tuple(str(x) for x in args.get("attachment_paths") or ())
        source = _inputs.get("artifact.pec_source")
        support = {
            "status": "not_applicable", "paths": [], "attachments": [],
            "local_staging_writes": 0, "writes": 0, "sends": 0,
        }
        gate = {"required": [], "missing": [], "gate": "none"}
        if isinstance(source, Mapping):
            with pec_gateway_factory() as reader:
                support = stage_tari_supporting_documents(reader, assignment.objective)
            gate = required_document_gate(source, explicit_paths)
        support_paths = tuple(str(x) for x in support.get("paths") or ())
        attachment_paths = tuple(dict.fromkeys(explicit_paths + support_paths))
        args["attachment_paths"] = list(attachment_paths)
        missing_requirements = tuple(str(x) for x in gate.get("missing") or ())
        if missing_requirements:
            result = {
                "ok": True,
                "status": "required_documents_missing",
                "missing_requirements": list(missing_requirements),
                "approval_required": True,
                "approval_created": False,
                "writes": 0,
                "sends": 0,
            }
        else:
            with pec_write_gateway_factory() as gateway:
                result = gateway.prepare(
                    recipient=str(args.get("recipient") or "") or None,
                    subject=str(args.get("subject") or "") or None,
                    body=str(args.get("body") or "") or None,
                    attachment_paths=attachment_paths,
                    requested_by=str(context.get("requested_by") or "bot-tazzi"),
                )
        return StructuredArtifact.create(
            artifact_type="pec_write_request",
            status=str(result.get("status") or "blocked"),
            producer_task_id=assignment.task_id,
            payload={
                **result,
                "draft": {
                    "recipient": str(args.get("recipient") or ""),
                    "subject": str(args.get("subject") or ""),
                    "body": str(args.get("body") or ""),
                    "attachment_paths": list(attachment_paths),
                },
                "supporting_documents": support,
                "required_document_gate": gate,
                "content_boundary": "pec_draft_not_sent",
                "writer_separate_from_reader": True,
                "writes": int(result.get("writes") or 0),
                "sends": int(result.get("sends") or 0),
            },
        )

    def document_sign_adapter(assignment, _inputs):
        operation = str(assignment.arguments.get("operation") or "prepare").strip().casefold()
        try:
            if operation == "prepare":
                source_path = str(assignment.arguments.get("source_path") or "").strip()
                if not source_path:
                    result = {
                        "ok": True,
                        "status": "clarification_required",
                        "message": "Serve il percorso esatto del documento da firmare; nessuna approval è stata creata.",
                        "writes": 0,
                        "sends": 0,
                    }
                else:
                    result = signing_workflow.prepare(
                        source_path,
                        requested_by=str(context.get("requested_by") or "bot-tazzi"),
                    )
                    if result.get("status") == "approval_required":
                        result["message"] = (
                            "Firma digitale pronta per approvazione: "
                            f"{result.get('source_path')}. Request {result.get('approval_request_id')} "
                            f"digest {result.get('scope_digest_short')}. "
                            "L'approvazione vale solo per questo hash. PIN/password/OTP saranno inseriti "
                            "direttamente in ArubaSign e non sono gestiti da Bot-tazzi."
                        )
            elif operation == "handoff":
                approval_id = str(assignment.arguments.get("approval_request_id") or "").strip()
                if not approval_id:
                    result = {
                        "ok": True, "status": "clarification_required",
                        "message": "Serve l'approval_request_id della firma approvata.",
                        "writes": 0, "sends": 0,
                    }
                else:
                    result = signing_workflow.handoff_approved(approval_id)
                    if result.get("status") == "user_interaction_required":
                        result["status"] = "clarification_required"
                        result["message"] = (
                            "ArubaSign richiede interazione utente per PIN/password/OTP. "
                            + ("L'applicazione è stata aperta sul documento approvato." if result.get("launched") else "Nessuna sessione grafica è disponibile su Sibilla: il documento resta approvato ma non è stato firmato.")
                        )
            elif operation == "verify":
                approval_id = str(assignment.arguments.get("approval_request_id") or "").strip()
                if not approval_id:
                    result = {
                        "ok": True, "status": "clarification_required",
                        "message": "Serve l'approval_request_id della firma da verificare.",
                        "writes": 0, "sends": 0,
                    }
                else:
                    result = signing_workflow.verify_approved_result(
                        approval_id,
                        str(assignment.arguments.get("signed_path") or "") or None,
                    )
                    if result.get("verified"):
                        result["status"] = "completed"
                        result["message"] = (
                            "Firma digitale verificata: integrità CMS valida, contenuto identico al documento "
                            "approvato e firmatario atteso corrispondente."
                        )
                    elif result.get("status") == "signed_artifact_not_found":
                        result["status"] = "clarification_required"
                        result["message"] = "Il file .p7m firmato non è ancora disponibile; nessuna firma è stata simulata."
                    else:
                        result["status"] = "unavailable"
                        result["message"] = "Il file firmato non supera la verifica crittografica/hash/identità."
            else:
                result = {
                    "ok": False, "status": "unavailable",
                    "message": "Operazione di firma non riconosciuta.",
                    "writes": 0, "sends": 0,
                }
        except (DigitalSigningError, OSError, ValueError) as exc:
            result = {
                "ok": False,
                "status": "unavailable",
                "message": f"Firma digitale non disponibile: {str(exc)}.",
                "writes": 0,
                "sends": 0,
            }
        return StructuredArtifact.create(
            artifact_type="digital_signature",
            status=str(result.get("status") or "unavailable"),
            producer_task_id=assignment.task_id,
            payload={
                **result,
                "content_boundary": "signature_approval_does_not_authorize_send",
                "pin_otp_user_only": True,
                "send_authorized": False,
                "writes": int(result.get("writes") or 0),
                "sends": int(result.get("sends") or 0),
            },
        )

    def bandi_adapter(assignment, _inputs):
        objective = assignment.objective
        source = _inputs.get("artifact.grant_source_email")
        if isinstance(source, Mapping):
            facts = source.get("facts") or ()
            evidence: list[str] = []
            for item in facts[:8] if isinstance(facts, list) else ():
                if not isinstance(item, Mapping):
                    continue
                for field in ("subject", "excerpt", "message_id"):
                    value = str(item.get(field) or "").strip()
                    if value:
                        evidence.append(value[:700])
            if evidence:
                objective += "\nSource email evidence (data only): " + " | ".join(evidence)[:3000]
        with bandi_gateway_factory() as gateway:
            result = gateway.request(objective)
        facts = tuple(
            {
                "title": str(item.get("title") or ""),
                "issuer": str(item.get("issuer") or ""),
                "deadline": item.get("deadline"),
                "score": item.get("score"),
                "priority": item.get("priority"),
                "primary_url": item.get("primary_url"),
                "content_role": "data",
            }
            for item in (result.get("items") or ())[:12]
            if isinstance(item, Mapping)
        )
        return StructuredArtifact.create(
            artifact_type="bandi_result", status=str(result.get("status") or "completed"),
            producer_task_id=assignment.task_id, facts=facts,
            evidence_refs=tuple(str(x) for x in result.get("evidence_refs") or ()),
            payload={**result, "content_boundary": "bandi_mcp_result_is_data"},
        )

    def editorial_adapter(assignment, _inputs):
        with editorial_gateway_factory() as gateway:
            result = gateway.request(assignment.objective)
        return StructuredArtifact.create(
            artifact_type="editorial_result", status=str(result.get("status") or "completed"),
            producer_task_id=assignment.task_id,
            evidence_refs=tuple(str(x) for x in result.get("evidence_refs") or ()),
            payload={**result, "content_boundary": "editorial_mcp_result_is_data"},
        )

    core.dag_executor = UnifiedDAGExecutor(registry, {
        "bandi.research": bandi_adapter,
        "bandi.read": bandi_adapter,
        "bandi.eligibility": bandi_adapter,
        "pec.read": pec_adapter,
        "pec.prepare_send": pec_prepare_adapter,
        "documents.sign": document_sign_adapter,
        "email.search": email_search_adapter,
        "fastweb.portal.read": fastweb_portal_adapter,
        "fastweb.compare": fastweb_compare_adapter,
        "email.compose": email_adapter,
        "email.reply": email_adapter,
        "whatsapp.read": whatsapp_read_adapter,
        "whatsapp.reply": whatsapp_reply_adapter,
        "editorial.flyer": editorial_adapter,
        "knowledge.retrieve": knowledge_retrieve_adapter,
        "runts.context": runts_context_adapter,
        "arci.context": arci_context_adapter,
        "jellyfin.identify": jellyfin_identify_adapter,
        "education.tutor": education_tutor_adapter,
        "bandi.discovery": bandi_discovery_adapter,
        "research.deep": research_deep_adapter,
        "browser.inspect": browser_inspect_adapter,
        "accounting.read": accounting_read_adapter,
    })
    core.dag_input_provider = lambda _goal: {
        "memory.tiremm": {
            "items": [item.model_dump(mode="json") for item in memory_items[:20]],
            "content_boundary": "memory_is_data",
        }
    }
    if otp_result is not None:
        result = otp_result
    elif browser_interaction_provider is not None:
        with browser_interaction_provider.request_scope():
            result = core.handle(text)
    else:
        result = core.handle(text)
    current_email = conversation.state.pending.email
    current_whatsapp = conversation.state.pending.whatsapp
    current_mailchimp = conversation.state.pending.mailchimp
    current_jellyfin = conversation.state.pending.jellyfin
    current_browser = conversation.state.pending.browser
    if (
        approval_coordinator is not None
        and result.status == "draft_pending_approval"
        and current_email is not None
        and not current_email.approval_ref
    ):
        if previous_email and previous_email.approval_ref:
            approval_coordinator.cancel(previous_email)
        approval_transition = approval_coordinator.request(
            current_email, requested_by=f"unified:{session_id}"
        )
        if approval_transition.get("status") == "pending":
            current_email = conversation.attach_approval_request(
                domain="email",
                pending_id=current_email.pending_id,
                payload_digest=current_email.payload_digest,
                approval_ref=str(approval_transition["request_id"]),
                created_at=int(approval_transition["created_at"]),
                expires_at=int(approval_transition["expires_at"]),
            )
            result.data.update({
                "approval_request_id": current_email.approval_ref,
                "approval_expires_at": current_email.expires_at,
            })
    elif (
        approval_coordinator is not None
        and result.status == "cancelled"
        and previous_email is not None
    ):
        approval_transition = approval_coordinator.cancel(previous_email)
    if (
        whatsapp_approval_coordinator is not None
        and result.status == "draft_pending_approval"
        and current_whatsapp is not None
        and not current_whatsapp.approval_ref
    ):
        if previous_whatsapp and previous_whatsapp.approval_ref:
            whatsapp_approval_coordinator.cancel(previous_whatsapp)
        approval_transition = whatsapp_approval_coordinator.request(
            current_whatsapp, requested_by=f"unified:{session_id}"
        )
        if approval_transition.get("status") == "pending":
            current_whatsapp = conversation.attach_approval_request(
                domain="whatsapp", pending_id=current_whatsapp.pending_id,
                payload_digest=current_whatsapp.payload_digest,
                approval_ref=str(approval_transition["request_id"]),
                created_at=int(approval_transition["created_at"]),
                expires_at=int(approval_transition["expires_at"]),
            )
            result.data.update({
                "approval_request_id": current_whatsapp.approval_ref,
                "approval_expires_at": current_whatsapp.expires_at,
            })
    elif (
        whatsapp_approval_coordinator is not None
        and result.status == "cancelled"
        and previous_whatsapp is not None
    ):
        approval_transition = whatsapp_approval_coordinator.cancel(previous_whatsapp)
    if (
        mailchimp_approval_coordinator is not None
        and result.status == "draft_pending_approval"
        and current_mailchimp is not None
        and not current_mailchimp.approval_ref
    ):
        if previous_mailchimp and previous_mailchimp.approval_ref:
            mailchimp_approval_coordinator.store.cancel(previous_mailchimp.approval_ref)
        approval_transition = mailchimp_approval_coordinator.request(
            current_mailchimp, requested_by=f"unified:{session_id}"
        )
        if approval_transition.get("status") == "pending":
            current_mailchimp = conversation.attach_approval_request(
                domain="mailchimp", pending_id=current_mailchimp.pending_id,
                payload_digest=current_mailchimp.payload_digest,
                approval_ref=str(approval_transition["request_id"]),
                created_at=int(approval_transition["created_at"]),
                expires_at=int(approval_transition["expires_at"]),
            )
            result.data.update({
                "approval_request_id": current_mailchimp.approval_ref,
                "approval_expires_at": current_mailchimp.expires_at,
            })
    if (
        jellyfin_approval_coordinator is not None
        and result.status == "protected_approval_required"
        and current_jellyfin is not None
        and not current_jellyfin.approval_ref
    ):
        if previous_jellyfin and previous_jellyfin.approval_ref:
            jellyfin_approval_coordinator.cancel(previous_jellyfin)
        approval_transition = jellyfin_approval_coordinator.request(
            current_jellyfin, requested_by=f"unified:{session_id}"
        )
        if approval_transition.get("status") == "pending":
            current_jellyfin = conversation.attach_approval_request(
                domain="jellyfin", pending_id=current_jellyfin.pending_id,
                payload_digest=current_jellyfin.payload_digest,
                approval_ref=str(approval_transition["request_id"]),
                created_at=int(approval_transition["created_at"]),
                expires_at=int(approval_transition["expires_at"]),
            )
            result.data.update({
                "approval_request_id": current_jellyfin.approval_ref,
                "approval_expires_at": current_jellyfin.expires_at,
            })
    elif (
        jellyfin_approval_coordinator is not None
        and result.status == "cancelled"
        and previous_jellyfin is not None
    ):
        approval_transition = jellyfin_approval_coordinator.cancel(previous_jellyfin)
    if (
        browser_approval_coordinator is not None
        and result.status == "draft_pending_approval"
        and current_browser is not None
        and not current_browser.approval_ref
    ):
        if previous_browser and previous_browser.approval_ref:
            browser_approval_coordinator.cancel(previous_browser)
        approval_transition = browser_approval_coordinator.request(
            current_browser, requested_by=f"unified:{session_id}"
        )
        if approval_transition.get("status") == "pending":
            current_browser = conversation.attach_approval_request(
                domain="browser", pending_id=current_browser.pending_id,
                payload_digest=current_browser.payload_digest,
                approval_ref=str(approval_transition["request_id"]),
                created_at=int(approval_transition["created_at"]),
                expires_at=int(approval_transition["expires_at"]),
            )
            result.data.update({
                "approval_request_id": current_browser.approval_ref,
                "approval_expires_at": current_browser.expires_at,
            })
    elif (
        browser_approval_coordinator is not None
        and result.status == "cancelled"
        and previous_browser is not None
    ):
        approval_transition = browser_approval_coordinator.cancel(previous_browser)
    if approval_transition:
        result.data["approval_transition"] = dict(approval_transition)
    session_adapter.save(session_id, conversation)
    artifacts: list[dict[str, Any]] = []
    if result.data.get("selected_skill") == "email.search":
        artifacts.append({
            "artifact_type": "email_search_result",
            "version": 1,
            "status": result.data.get("search_status"),
            "evidence_status": result.data.get("evidence_status"),
            "search_complete": result.data.get("search_complete"),
            "searched_scope": result.data.get("searched_scope"),
            "pages_read": result.data.get("pages_read"),
            "messages_seen": result.data.get("messages_seen"),
            "messages_hydrated": result.data.get("messages_hydrated"),
            "dedup_count": result.data.get("dedup_count"),
            "cap_reached": result.data.get("cap_reached"),
            "evidence_refs": list(result.data.get("provenance") or ()),
            "content_role": "data",
            "side_effects": 0,
        })
    elif result.data.get("selected_skill") == "fastweb.portal.read":
        portal = result.data.get("portal") or {}
        artifacts.append({
            "artifact_type": "fastweb_portal_state",
            "version": 1,
            "status": portal.get("status"),
            "offer_name": portal.get("offer_name"),
            "current_fee": portal.get("current_fee"),
            "effective_date": portal.get("effective_date"),
            "evidence_refs": list(portal.get("provenance") or ()),
            "content_role": "data",
            "side_effects": 0,
        })
    elif result.data.get("selected_skill") == "atm.route":
        atm = result.data.get("atm") or {}
        artifacts.append({
            "artifact_type": "atm_route_result",
            "version": 1,
            "status": result.status,
            "tool": result.data.get("atm_tool"),
            "destination": atm.get("destination"),
            "duration_min": atm.get("duration_min"),
            "walking_m": atm.get("walking_m"),
            "lines": atm.get("lines"),
            "destination_eta": atm.get("destination_eta"),
            "location_source": result.data.get("location_source"),
            "source": atm.get("source"),
            "content_role": "data",
            "side_effects": 0,
        })
    elif result.data.get("selected_skill") == "meteo.read":
        meteo = result.data.get("meteo") or {}
        artifacts.append({
            "artifact_type": "meteo_read_result",
            "version": 1,
            "status": result.status,
            "tool": result.data.get("meteo_tool"),
            "location": meteo.get("location"),
            "location_source": result.data.get("location_source"),
            "current": meteo.get("current"),
            "next_hours": list(meteo.get("next_hours") or ()),
            "radar_url": meteo.get("radar_url"),
            "content_role": "data",
            "persistent_memory_writes": 0,
            "side_effects": 0,
            "writes": 0,
            "sends": 0,
        })
    elif result.data.get("selected_skill") == "mailchimp.read":
        mailchimp = result.data.get("mailchimp") or {}
        artifacts.append({
            "artifact_type": "mailchimp_read_result",
            "version": 1,
            "status": result.status,
            "operation": result.data.get("mailchimp_operation"),
            "tool": result.data.get("mailchimp_tool"),
            "results": list(mailchimp.get("results") or ()),
            "audiences": list(mailchimp.get("audiences") or ()),
            "segments": list(mailchimp.get("segments") or ()),
            "tags": list(mailchimp.get("tags") or ()),
            "analysis_complete": mailchimp.get("analysis_complete"),
            "health_status": mailchimp.get("health_status"),
            "count": mailchimp.get("count"),
            "offset": mailchimp.get("offset"),
            "content_role": "data",
            "persistent_memory_writes": 0,
            "side_effects": 0,
            "writes": 0,
            "sends": 0,
        })
    elif result.data.get("selected_skill") == "whatsapp.read":
        whatsapp = result.data.get("whatsapp") or {}
        artifacts.append({
            "artifact_type": "whatsapp_visible_context",
            "version": 1,
            "status": whatsapp.get("status"),
            "namespace": whatsapp.get("namespace"),
            "chat_ref": whatsapp.get("chat_ref"),
            "visible_items": list(whatsapp.get("visible_items") or ()),
            "media_items": list(whatsapp.get("media_items") or ()),
            "evidence_refs": list(whatsapp.get("provenance") or ()),
            "content_role": "data",
            "persistent_memory_writes": 0,
            "side_effects": 0,
        })
    return {
        "ok": result.status not in {"blocked", "denied", "unavailable"},
        "interaction_mode": "unified_assistant",
        "capability": result.data.get("selected_skill") or "unified_assistant",
        "tools_executed": bool(result.data.get("tools_executed", False)),
        "response": result.message,
        "final_answer": result.message,
        "approval_required": result.status in {
            "draft_pending_approval", "confirmation_required", "protected_approval_required",
            "approval_required",
        },
        "pending_confirmation_id": result.data.get("pending_id"),
        "approval_request_id": result.data.get("approval_request_id"),
        "scope_digest_short": result.data.get("scope_digest_short"),
        "source_sha256": result.data.get("source_sha256"),
        "signed_path": result.data.get("signed_path"),
        "signature_verified": result.data.get("signature_verified"),
        "pin_otp_user_only": result.data.get("pin_otp_user_only"),
        "send_authorized": result.data.get("send_authorized"),
        "writes": int(result.data.get("writes") or 0),
        "sends": int(result.data.get("sends") or 0),
        "metadata": {"status": result.status, **result.data},
        "artifacts": artifacts,
        "audit_summary": [f"unified_assistant::{result.status}"],
    }



_RUNTS_EXPLICIT_APPROVAL = re.compile(
    r"^\s*approvo\s+(?P<practice>[0-9]{1,24})\s*[.!]?\s*$",
    re.I,
)


def _session_store() -> SessionStore:
    return SessionStore(os.getenv(
        "RALFLOOP_UNIFIED_SESSION_DIR",
        str(
            Path.home()
            / ".local"
            / "state"
            / "ralf"
            / "unified-sessions"
        ),
    ))


def _runts_reader():
    return RuntsAuthenticatedBrowserAdapter(
        RuntsAuthenticatedCdpTransport()
    )


def _runts_writer():
    return RuntsAuthenticatedCdpWriteTransport()


def _is_explicit_runts_approval(
    text: str,
    context: Mapping[str, Any],
) -> bool:
    match = _RUNTS_EXPLICIT_APPROVAL.fullmatch(
        text
    )

    if match is None:
        return False

    try:
        session_id = _session_id(context)
        conversation = SessionConversationAdapter(
            _session_store()
        ).load(session_id)

    except (
        OSError,
        SessionStoreError,
        TypeError,
        ValueError,
    ):
        return False

    pending = conversation.state.pending.runts

    if (
        pending is None
        or pending.action != RUNTS_REPLY_ACTION
        or pending.expires_at
        <= int(datetime.now(UTC).timestamp())
        or not payload_matches(pending)
    ):
        return False

    return (
        str(
            pending.payload.get(
                "practice_id"
            )
            or ""
        )
        == match.group("practice")
    )


def _stage_runts_prepare_for_approval(
    result: Mapping[str, Any],
    context: Mapping[str, Any],
) -> dict[str, Any]:
    session_id = _session_id(context)

    store = _session_store()
    _ensure_session(store, session_id)

    adapter = SessionConversationAdapter(store)
    conversation = adapter.load(session_id)

    metadata = dict(
        result.get("metadata") or {}
    )

    proposal = metadata.get("proposal")

    if not isinstance(proposal, Mapping):
        return dict(result)

    practice_status = str(
        metadata.get("practice_status")
        or ""
    )

    if not practice_status:
        failed = dict(result)
        failed["ok"] = False
        failed["approval_required"] = False
        failed["response"] = (
            "PREPARE RUNTS completato, ma lo stato "
            "autoritativo della pratica non è stato "
            "congelato. Nessun WRITE eseguito."
        )
        failed["final_answer"] = failed["response"]
        return failed

    payload = build_runts_pending_payload(
        proposal,
        expected_practice_status=practice_status,
    )

    practice_id = str(
        payload["practice_id"]
    )

    display = (
        str(result.get("response") or "")
        + "\n\n"
        + "Azione proposta: risposta nella "
          "MESSAGGISTICA della pratica "
        + practice_id
        + "."
        + "\nOggetto: "
        + str(payload["subject"])
        + "\nMessaggio proposto (testo vincolato all'approvazione):\n"
        + str(payload["body"])
        + "\nAllegato: "
        + str(payload["pdf_name"])
        + "\nSHA256: "
        + str(payload["pdf_sha256"])
        + "\nTipo documento: BILANCIO "
          "D'ESERCIZIO (B00)"
        + "\n\nPer approvare esattamente questa "
          "azione scrivi:"
        + "\nApprovo "
        + practice_id
    )

    previous = conversation.state.pending.runts

    pending = conversation.stage(
        domain="runts",
        action=RUNTS_REPLY_ACTION,
        policy=PolicyClass.CONFIRM_WRITE,
        payload=payload,
        displayed_text=display,
    )

    policy = DomainApprovalPolicy.from_env()

    approval_transition = {
        "status": "approval_gate_disabled"
    }

    if policy.enabled:
        approval_store = DomainApprovalStore(
            policy=policy
        )

        coordinator = UnifiedRuntsApprovalCoordinator(
            approval_store,
            policy=policy,
        )

        if (
            previous is not None
            and previous.approval_ref
        ):
            coordinator.cancel(previous)

        approval_transition = coordinator.request(
            pending,
            requested_by=(
                "unified:" + session_id
            ),
        )

        if (
            approval_transition.get("status")
            == "pending"
        ):
            pending = (
                conversation
                .attach_approval_request(
                    domain="runts",
                    pending_id=pending.pending_id,
                    payload_digest=(
                        pending.payload_digest
                    ),
                    approval_ref=str(
                        approval_transition[
                            "request_id"
                        ]
                    ),
                    created_at=int(
                        approval_transition[
                            "created_at"
                        ]
                    ),
                    expires_at=int(
                        approval_transition[
                            "expires_at"
                        ]
                    ),
                )
            )

    adapter.save(
        session_id,
        conversation,
    )

    output = dict(result)

    output.update({
        "response": display,
        "final_answer": display,
        "approval_required": True,
        "human_review_required": True,
        "pending_confirmation_id":
            pending.pending_id,
    })

    output_metadata = dict(
        output.get("metadata") or {}
    )

    output_metadata.update({
        "pending_domain": "runts",
        "pending_action":
            RUNTS_REPLY_ACTION,
        "pending_id":
            pending.pending_id,
        "pending_digest":
            pending.payload_digest,
        "approval_request_id":
            pending.approval_ref,
        "approval_transition":
            dict(approval_transition),
        "writes": 0,
    })

    output["metadata"] = output_metadata

    output["audit_summary"] = [
        *list(
            output.get(
                "audit_summary"
            )
            or ()
        ),
        "RUNTS_PENDING_APPROVAL_CREATED",
    ]

    return output


def _execute_explicit_runts_approval(
    text: str,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    match = _RUNTS_EXPLICIT_APPROVAL.fullmatch(
        text
    )

    if match is None:
        raise ValueError(
            "runts_explicit_approval_required"
        )

    session_id = _session_id(context)

    session_store = _session_store()
    _ensure_session(
        session_store,
        session_id,
    )

    adapter = SessionConversationAdapter(
        session_store
    )

    conversation = adapter.load(
        session_id
    )

    now = int(datetime.now(UTC).timestamp())
    active = [
        item
        for name in PENDING_DOMAINS
        if (
            item := getattr(
                conversation.state.pending,
                name,
            )
        )
        is not None
        and item.expires_at > now
        and payload_matches(item)
    ]

    if (
        len(active) != 1
        or active[0].domain != "runts"
    ):
        return {
            "ok": False,
            "interaction_mode":
                "unified_assistant",
            "capability":
                "runts_practice_reply",
            "tools_executed": False,
            "response":
                "Approvazione RUNTS ambigua o "
                "non più disponibile.",
            "final_answer":
                "Approvazione RUNTS ambigua o "
                "non più disponibile.",
            "approval_required": True,
            "metadata": {
                "status":
                    "clarification_required",
                "writes": 0,
            },
            "artifacts": [],
            "audit_summary": [
                "RUNTS_APPROVAL_NOT_RESOLVED"
            ],
        }

    pending = active[0]

    if (
        str(
            pending.payload.get(
                "practice_id"
            )
            or ""
        )
        != match.group("practice")
    ):
        return {
            "ok": False,
            "interaction_mode":
                "unified_assistant",
            "capability":
                "runts_practice_reply",
            "tools_executed": False,
            "response":
                "La pratica indicata non "
                "corrisponde all'azione RUNTS "
                "in attesa.",
            "final_answer":
                "La pratica indicata non "
                "corrisponde all'azione RUNTS "
                "in attesa.",
            "approval_required": True,
            "metadata": {
                "status":
                    "practice_mismatch",
                "writes": 0,
            },
            "artifacts": [],
            "audit_summary": [
                "RUNTS_APPROVAL_PRACTICE_MISMATCH"
            ],
        }

    policy = DomainApprovalPolicy.from_env()

    if not policy.enabled:
        return {
            "ok": False,
            "interaction_mode":
                "unified_assistant",
            "capability":
                "runts_practice_reply",
            "tools_executed": False,
            "response":
                "Gate di approvazione Telegram "
                "non abilitato; nessun WRITE "
                "RUNTS eseguito.",
            "final_answer":
                "Gate di approvazione Telegram "
                "non abilitato; nessun WRITE "
                "RUNTS eseguito.",
            "approval_required": True,
            "metadata": {
                "status":
                    "approval_gate_disabled",
                "writes": 0,
            },
            "artifacts": [],
            "audit_summary": [
                "RUNTS_APPROVAL_GATE_DISABLED"
            ],
        }

    approval_store = DomainApprovalStore(
        policy=policy
    )

    coordinator = UnifiedRuntsApprovalCoordinator(
        approval_store,
        policy=policy,
    )

    transition = coordinator.approve(
        pending,
        telegram_user_id=int(
            context.get(
                "telegram_user_id"
            )
            or 0
        ),
        telegram_chat_id=int(
            context.get(
                "telegram_chat_id"
            )
            or 0
        ),
        telegram_message_id=int(
            context.get(
                "telegram_message_id"
            )
            or 0
        ),
        chat_type=str(
            context.get(
                "telegram_chat_type"
            )
            or "private"
        ),
    )

    if transition.get("status") not in {
        "approved",
        "already_approved",
    }:
        adapter.save(
            session_id,
            conversation,
        )

        status = str(
            transition.get("status")
            or "approval_failed"
        )

        answer = (
            "Approvazione RUNTS non accettata: "
            + status
            + ". Nessun WRITE eseguito."
        )

        return {
            "ok": False,
            "interaction_mode":
                "unified_assistant",
            "capability":
                "runts_practice_reply",
            "tools_executed": True,
            "response": answer,
            "final_answer": answer,
            "approval_required": True,
            "metadata": {
                "status": status,
                "approval_transition":
                    dict(transition),
                "writes": 0,
            },
            "artifacts": [],
            "audit_summary": [
                "RUNTS_APPROVAL_REJECTED"
            ],
        }

    pending = conversation.bind_approval(
        domain="runts",
        pending_id=pending.pending_id,
        payload_digest=pending.payload_digest,
        approval_ref=str(
            pending.approval_ref
        ),
    )

    executor = RuntsApprovedReplyExecutor(
        store=approval_store,
        reader_factory=_runts_reader,
        writer_factory=_runts_writer,
    )

    execution = executor.execute(
        pending
    )

    status = str(
        execution.get("status")
        or "failed"
    )

    terminal = {
        "EXECUTED_VERIFIED",
        "already_executed",
        "EXECUTION_UNCERTAIN",
        "FAILED_AFTER_PARTIAL_WRITE",
    }

    if status in terminal:
        conversation.clear("runts")

    adapter.save(
        session_id,
        conversation,
    )

    if status == "runts_write_disabled":
        answer = (
            "Approvazione RUNTS registrata e "
            "hash-bound. Il WRITE è disabilitato "
            "da BOTTAZZI_RUNTS_WRITE_ENABLED=0: "
            "nessun invio eseguito."
        )

    elif status == "EXECUTED_VERIFIED":
        answer = (
            "Risposta RUNTS inviata e verificata "
            "nella pratica "
            + str(
                pending.payload[
                    "practice_id"
                ]
            )
            + "."
        )

    elif status == "already_executed":
        answer = (
            "La risposta RUNTS risulta già "
            "eseguita; nessun secondo invio."
        )

    elif status == "EXECUTION_UNCERTAIN":
        answer = (
            "Esito RUNTS non certo. Retry "
            "automatico bloccato."
        )

    elif status == "FAILED_AFTER_PARTIAL_WRITE":
        answer = (
            "RUNTS ha registrato una scrittura "
            "parziale; retry automatico bloccato."
        )

    else:
        answer = (
            "Risposta RUNTS non eseguita. Stato: "
            + status
            + "."
        )

    return {
        "ok": status in {
            "runts_write_disabled",
            "EXECUTED_VERIFIED",
            "already_executed",
        },
        "interaction_mode":
            "unified_assistant",
        "capability":
            "runts_practice_reply",
        "tools_executed": True,
        "response": answer,
        "final_answer": answer,
        "approval_required":
            status == "runts_write_disabled",
        "metadata": {
            "status": status,
            "approval_transition":
                dict(transition),
            "execution":
                dict(execution),
            "writes":
                int(
                    execution.get(
                        "writes"
                    )
                    or 0
                ),
        },
        "artifacts": [],
        "audit_summary": [
            "RUNTS_APPROVAL_BOUND_EXECUTION::"
            + status
        ],
    }


def _session_id(context: Mapping[str, Any]) -> str:
    if str(context.get("source") or "") == "ralf_terminal":
        terminal = context.get("terminal_client") or {}
        value = str(terminal.get("session_id") or "").strip()
        if not value:
            raise ValueError("terminal_identity_required")
        return f"terminal-{value}"[:128]
    chat = int(context.get("telegram_chat_id") or 0)
    user = int(context.get("telegram_user_id") or 0)
    if chat <= 0 or user <= 0:
        raise ValueError("telegram_identity_required")
    return f"telegram-{chat}-{user}"[:128]


def _ensure_session(store: SessionStore, session_id: str) -> None:
    try:
        store.load(session_id)
        return
    except SessionStoreError as exc:
        if str(exc) != "session_not_found":
            raise
    now = datetime.now(UTC).isoformat()
    record = {
        "session_id": session_id, "created_at": now, "updated_at": now,
        "cwd": str(Path.home()), "history": [], "model": None,
        "context_enabled": True, "metadata": {},
    }
    store.save(record)


def _is_positive_confirmation(text: str) -> bool:
    folded = " ".join(text.casefold().split()).strip(" .!?")
    return folded in CONFIRM_WORDS


def _is_pec_runts_request(text: str) -> bool:
    from .pec_runts_telegram import PecInboxDecision, decision_for_telegram

    decision = decision_for_telegram(text)
    return decision is not None and not isinstance(decision, PecInboxDecision)


__all__ = ["is_unified_telegram_request", "run_unified_telegram", "unified_route_probe"]
