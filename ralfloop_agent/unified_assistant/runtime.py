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
from .executor import StructuredArtifact, UnifiedDAGExecutor
from .fastweb_portal import FastwebPortalReadOnly
from .home import HomeEntityRegistry, HomeWorkflow
from .home_provider import HomeAssistantProviderError, HomeAssistantRESTBackend
from .memory import MemoryRouter, tiremm_profile_items
from .atm_mcp_adapter import ATMMCPReadOnly
from .editorial_mcp_adapter import EditorialMCPContext
from .meteo_mcp_adapter import MeteoMCPReadOnly
from .planner import UnifiedPlanner
from .capability_rag_router import CapabilityRAGRouter
from .recipient import GoogleWorkspaceRecipientResolver
from .registry import DEFAULT_HOME_ENTITIES, UnifiedRegistryFacade
from .skill_adapters import bandi_eligibility_adapter, bandi_read_adapter
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
    r"manda\s+(?:una\s+)?(?:mail|email)|rispondi\s+(?:a|alla\s+mail(?:\s+di)?)|accendi|spegni|apri|chiudi|"
    r"imposta|metti|porta|abbassala|alzala|temperatura|quanto\s+fa|fa\s+caldo|"
    r"fa\s+freddo|rendila|cambiala|aggiungi|modifica|ok|invia|mandala|va\s+bene|annulla|"
    r"fastweb|myfastpage|whatsapp|wapp|mailchimp|meteo|weather|previsioni|piove|piover[aà]|pioggia|"
    r"temporale|radar|precipitazioni|vento|atm|giromilano|"
    r"mezzi\s+pubblici|trasporto\s+pubblico|portami|volantin[oi]|flyer|locandin[ae]|manifest[oi]|poster|"
    r"come\s+(?:arrivo|vado|posso\s+andare)|"
    r"mezzi\s+(?:per|verso)|percorso\s+(?:atm|con\s+i\s+mezzi)|home\s+assistant|domotica|stato\s+(?:della\s+)?luce)\b",
    re.I,
)

_EMAIL_READ_SUPPORTED = re.compile(
    r"\b(?:controlla|cerca|trova|verifica|guarda|leggi|abbiamo\s+ricevuto|ha\s+mai|ci\s+ha|ci\s+aveva)\b.*"
    r"\b(?:mail|email|posta|bozz[ae]|scritto|comunicat[oaie]|comunicazioni|avvisat[oaie]|messaggi?|thread)\b",
    re.I,
)


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
        and active[0].domain in {"email", "whatsapp", "mailchimp"}
        and active[0].policy.value in {"CONFIRM_WRITE", "PROTECTED"}
        and bool(active[0].approval_ref)
        and payload_matches(active[0])
    )


def unified_route_probe(text: str, context: Mapping[str, Any]) -> dict[str, Any] | None:
    """Side-effect-free route metadata for Meowgram's existing two-step contract."""

    if not is_unified_telegram_request(text, context):
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
    if (
        "email.search" in skills
        or "fastweb.portal.read" in skills
        or "whatsapp.read" in skills
        or "mailchimp.read" in skills
        or "meteo.read" in skills
        or "atm.route" in skills
    ):
        task_mode = "tool_backed_read"
        interaction_class = "TOOL_BACKED_READ"
        connectors = []
        if "email.search" in skills:
            connectors.append("google_workspace.gmail")
        if "fastweb.portal.read" in skills:
            connectors.append("fastweb.portal.read_only")
        if "whatsapp.read" in skills:
            connectors.append("whatsapp.web.mcp")
        if "mailchimp.read" in skills:
            connectors.append("mailchimp.marketing")
        if "meteo.read" in skills:
            connectors.append("meteo.radar.mcp")
        if "atm.route" in skills:
            connectors.append("atm.route.mcp")
    elif all(item.policy.value == "READ" for item in plan.assignments):
        task_mode = "tool_backed_read"
        interaction_class = "TOOL_BACKED_READ"
        connectors = []
    else:
        task_mode = "external_action"
        interaction_class = "EXTERNAL_ACTION"
        connectors = []
        if any(item.domain == "email" for item in plan.assignments):
            connectors.append("google_workspace.gmail")
        if any(item.domain == "whatsapp" for item in plan.assignments):
            connectors.append("whatsapp.web.mcp")
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


def run_unified_telegram(text: str, context: Mapping[str, Any]) -> dict[str, Any]:
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

    flags = AssistantFeatureFlags.from_env()
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
    home_workflow = None
    if flags.home_assistant_read_live or flags.home_assistant_live:
        try:
            home_workflow = HomeWorkflow(
                HomeEntityRegistry.load(DEFAULT_HOME_ENTITIES),
                HomeAssistantRESTBackend.from_environment(),
            )
        except (OSError, ValueError, HomeAssistantProviderError):
            home_workflow = None
    approval_coordinator = None
    approval_executor = None
    whatsapp_approval_coordinator = None
    whatsapp_approval_executor = None
    mailchimp_approval_coordinator = None
    mailchimp_approval_executor = None
    if flags.email_assistant_live or flags.whatsapp_assistant_live or flags.mailchimp_campaign_live:
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
    previous_email = conversation.state.pending.email
    previous_whatsapp = conversation.state.pending.whatsapp
    previous_mailchimp = conversation.state.pending.mailchimp
    approval_transition: dict[str, Any] = {}
    if any((approval_coordinator, whatsapp_approval_coordinator, mailchimp_approval_coordinator)) and _is_positive_confirmation(text):
        active = [
            item for name in PENDING_DOMAINS
            if (item := getattr(conversation.state.pending, name)) is not None
        ]
        if len(active) == 1 and active[0].domain in {"email", "whatsapp", "mailchimp"}:
            pending = active[0]
            coordinator = ({
                "email": approval_coordinator,
                "whatsapp": whatsapp_approval_coordinator,
                "mailchimp": mailchimp_approval_coordinator,
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
        request = (
            f"Cerca le mail di {organization} riguardo comunicazioni pertinenti"
            if organization and assignment.arguments.get("concept") == "communications" else
            f"Controlla se {organization} ha mai comunicato un aumento"
            if organization else assignment.objective
        )
        search = core.email_search.search(request)
        return StructuredArtifact.create(
            artifact_type="email_search_result", status=search.status,
            producer_task_id=assignment.task_id,
            facts=tuple({
                "date": item.date, "sender": item.sender, "subject": item.subject,
                "matched_terms": list(item.matched_terms), "content_role": "data",
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
        "bandi.read": bandi_read_adapter,
        "bandi.eligibility": bandi_eligibility_adapter,
        "email.search": email_search_adapter,
        "fastweb.portal.read": fastweb_portal_adapter,
        "fastweb.compare": fastweb_compare_adapter,
        "email.compose": email_adapter,
        "email.reply": email_adapter,
        "whatsapp.read": whatsapp_read_adapter,
        "whatsapp.reply": whatsapp_reply_adapter,
        "editorial.flyer": editorial_adapter,
    })
    core.dag_input_provider = lambda _goal: {
        "memory.tiremm": {
            "items": [item.model_dump(mode="json") for item in memory_items[:20]],
            "content_boundary": "memory_is_data",
        }
    }
    result = otp_result or core.handle(text)
    current_email = conversation.state.pending.email
    current_whatsapp = conversation.state.pending.whatsapp
    current_mailchimp = conversation.state.pending.mailchimp
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
    from .pec_runts_telegram import is_pec_runts_telegram

    return is_pec_runts_telegram(text)


__all__ = ["is_unified_telegram_request", "run_unified_telegram", "unified_route_probe"]
