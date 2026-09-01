from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import re
from typing import Any, Mapping

from ralfloop_agent.cli.session_store import SessionStore, SessionStoreError
from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from .contracts import AssistantFeatureFlags
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
from .planner import UnifiedPlanner
from .recipient import GoogleWorkspaceRecipientResolver
from .registry import DEFAULT_HOME_ENTITIES, UnifiedRegistryFacade
from .skill_adapters import bandi_eligibility_adapter, bandi_read_adapter
from .whatsapp_compose import EmailBackedWhatsAppDraftPipeline, UnifiedWhatsAppComposeService
from .whatsapp_mcp_adapter import WhatsAppMCPReadOnly
from .whatsapp_send import (
    UnifiedWhatsAppApprovalCoordinator, UnifiedWhatsAppApprovalExecutor,
)
from src.mailchimp import MailchimpApprovedMCPWorkflow, MailchimpMCPContext
from src.whatsapp import WhatsAppMCPContext


_LEGACY = re.compile(r"^(?:/|rl:|rsc\b|abc\b|atm\b|apri\s+cancello\b)", re.I)
_SUPPORTED = re.compile(
    r"\b(?:scrivi\s+(?:una\s+mail\s+)?a|prepara\s+(?:una\s+)?(?:mail|email)|"
    r"manda\s+(?:una\s+)?(?:mail|email)|rispondi\s+(?:a|alla\s+mail(?:\s+di)?)|accendi|spegni|apri|chiudi|"
    r"imposta|metti|porta|abbassala|alzala|temperatura|quanto\s+fa|fa\s+caldo|"
    r"fa\s+freddo|rendila|cambiala|aggiungi|modifica|ok|invia|mandala|va\s+bene|annulla|"
    r"fastweb|myfastpage|whatsapp|wapp|mailchimp)\b",
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
    return (
        flags.unified_assistant
        and (source.startswith("telegram_") or source == "ralf_terminal")
        and not _LEGACY.search(text.strip())
        and bool(
            _SUPPORTED.search(text)
            or _EMAIL_READ_SUPPORTED.search(text)
            or re.fullmatch(r"\s*(?:otp[\s:-]*)?[0-9]{6}\s*", text, re.I)
            or (
                _is_positive_confirmation(text)
                and _has_single_approvable_pending(context)
            )
        )
    )


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
    if re.fullmatch(r"\s*(?:otp[\s:-]*)?[0-9]{6}\s*", text, re.I):
        return {
            "task_mode": "external_action", "mode": "external_action",
            "interaction_class": "EXTERNAL_ACTION", "intent": "email.otp",
            "arguments": {}, "domains": ["email"],
            "skills_used": ["email.approve"], "domain_skills": ["email.approve"],
            "mcp_used": [], "mcp_connectors": [], "write_policy": "policy_gated",
            "evidence_first": True, "requires_confirmation": True,
        }
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    plan = planner.validate(planner.plan(text))
    skills = [item.skill for item in plan.assignments]
    if (
        "email.search" in skills
        or "fastweb.portal.read" in skills
        or "whatsapp.read" in skills
        or "mailchimp.read" in skills
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
        planner=UnifiedPlanner(registry),
        conversation=conversation,
        flags=flags,
        email_memory=email_memory_builder,
        email_pipeline=email_pipeline,
        email_search=GoogleWorkspaceEmailSearch.from_environment(),
        fastweb_portal=fastweb_portal,
        whatsapp_read=whatsapp_read,
        mailchimp_gateway_factory=mailchimp_gateway_factory,
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


def _session_id(context: Mapping[str, Any]) -> str:
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


__all__ = ["is_unified_telegram_request", "run_unified_telegram", "unified_route_probe"]
