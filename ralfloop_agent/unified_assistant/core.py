from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Callable, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .contracts import AssistantFeatureFlags, PolicyClass
from .conversation import (
    ConversationManager,
    PendingAction,
    approval_matches,
    payload_matches,
)
from .email import EmailWorkingContext, EmailWorkingMemoryBuilder, pending_email_payload
from .email_search import EmailSearchResult
from .executor import UnifiedDAGExecutor
from .fastweb_portal import FastwebPortalResult
from .home import HomeIntentParser, HomePreparedAction, HomeWorkflow
from .memory import MemoryRouter
from .planner import UnifiedPlanner
from .whatsapp_web import WhatsAppReadResult
from .whatsapp_compose import UnifiedWhatsAppComposeService


class EmailPipelineResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    body: str = Field(min_length=1, max_length=20_000)
    hard_guard: str
    risk: str
    ds4_invoked: bool = False
    repair_count: int = Field(default=0, ge=0, le=1)
    final_validator: str
    trace: dict[str, Any] = Field(default_factory=dict)


class EmailPipeline(Protocol):
    def compose(self, working: EmailWorkingContext) -> EmailPipelineResult: ...

    def revise(
        self, working: EmailWorkingContext, current_body: str, instruction: str
    ) -> EmailPipelineResult: ...


class RecipientResolver(Protocol):
    def resolve(self, label: str) -> dict[str, str] | None: ...


class EmailSearchService(Protocol):
    def search(self, request: str) -> EmailSearchResult: ...


class FastwebPortalService(Protocol):
    def read(self) -> FastwebPortalResult: ...


class WhatsAppReadService(Protocol):
    def read(self, request: str, *, allowed_namespaces: tuple[str, ...]) -> WhatsAppReadResult: ...


class ApprovalBoundExecutor(Protocol):
    def execute(self, pending: PendingAction) -> dict[str, Any]: ...


@dataclass(frozen=True)
class UnifiedAssistantResult:
    status: str
    message: str
    data: dict[str, Any]


class UnifiedAssistantCore:
    """Conversation facade. Executors are injected, allowlisted and deterministic."""

    def __init__(
        self,
        *,
        planner: UnifiedPlanner,
        conversation: ConversationManager,
        flags: AssistantFeatureFlags,
        email_memory: EmailWorkingMemoryBuilder | None = None,
        email_pipeline: EmailPipeline | None = None,
        email_search: EmailSearchService | None = None,
        fastweb_portal: FastwebPortalService | None = None,
        whatsapp_read: WhatsAppReadService | None = None,
        whatsapp_compose: UnifiedWhatsAppComposeService | None = None,
        recipient_resolver: RecipientResolver | None = None,
        approval_executor: ApprovalBoundExecutor | None = None,
        whatsapp_approval_executor: ApprovalBoundExecutor | None = None,
        home_workflow: HomeWorkflow | None = None,
        dag_executor: UnifiedDAGExecutor | None = None,
        dag_input_provider: Callable[[str], Mapping[str, Any]] | None = None,
        memory_router: MemoryRouter | None = None,
    ) -> None:
        self.planner = planner
        self.conversation = conversation
        self.flags = flags
        self.email_memory = email_memory
        self.email_pipeline = email_pipeline
        self.email_search = email_search
        self.fastweb_portal = fastweb_portal
        self.whatsapp_read = whatsapp_read
        self.whatsapp_compose = whatsapp_compose
        self.recipient_resolver = recipient_resolver
        self.approval_executor = approval_executor
        self.whatsapp_approval_executor = whatsapp_approval_executor
        self.home_workflow = home_workflow
        self.dag_executor = dag_executor
        self.dag_input_provider = dag_input_provider
        self.memory_router = memory_router
        self.home_parser = HomeIntentParser()
        self.audit_events: list[dict[str, Any]] = []
        self._email_working: dict[str, EmailWorkingContext] = {}
        self._home_prepared: dict[str, HomePreparedAction] = {}

    def handle(self, text: str, *, domain_hint: str | None = None) -> UnifiedAssistantResult:
        if not self.flags.unified_assistant:
            return self._result("disabled", "Unified assistant disabled.")
        confirmation = self.conversation.resolve_confirmation(text, domain_hint=domain_hint)
        if confirmation.status == "ambiguous":
            return self._result(
                "clarification_required",
                "Conferma ambigua: specifica l'azione.",
                domains=list(confirmation.domains),
            )
        if confirmation.status == "no_pending":
            return self._result("no_pending_action", "Nessuna azione in attesa.")
        if confirmation.status == "cancelled":
            return self._result("cancelled", "Azione annullata.")
        if confirmation.status == "expired":
            return self._result("approval_expired", "Bozza scaduta; prepara una nuova bozza.")
        if confirmation.status == "resolved" and confirmation.pending:
            return self._execute_pending(confirmation.pending)

        if _is_email_revision(text) and self.conversation.state.pending.email:
            return self._revise_email(text)
        if _is_email_revision(text) and self.conversation.state.pending.whatsapp:
            return self._revise_whatsapp(text)

        plan = self.planner.validate(self.planner.plan(text))
        if plan.intent == "assistant.reject":
            return self._result("denied", "Richiesta vietata dalla policy.", plan=plan.model_dump(mode="json"))
        if len(plan.assignments) > 1:
            if self.dag_executor is not None:
                inputs = {"user.goal": text}
                if self.dag_input_provider is not None:
                    inputs.update(self.dag_input_provider(text))
                execution = self.dag_executor.execute(plan, inputs=inputs)
                artifact_rows = execution.artifacts
                preview = next(
                    (str(item.payload.get("message") or "") for item in reversed(artifact_rows)
                     if item.artifact_type in {"email_draft", "whatsapp_draft", "fastweb_comparison"}),
                    "",
                )
                has_draft = any(
                    item.artifact_type in {"email_draft", "whatsapp_draft"}
                    for item in artifact_rows
                )
                return self._result(
                    "draft_pending_approval" if preview and has_draft and execution.status == "completed" else
                    ("multi_domain_completed" if execution.status == "completed" else "blocked"),
                    preview or ("Piano multi-domain eseguito." if execution.status == "completed" else "Piano multi-domain bloccato."),
                    plan=plan.model_dump(mode="json"), execution=execution.model_dump(mode="json"),
                )
            return self._result(
                "planned",
                "Piano multi-domain validato; esecuzione non automatica.",
                plan=plan.model_dump(mode="json"),
            )
        assignment = plan.assignments[0]
        if assignment.skill == "email.search":
            return self._search_email(text, plan.model_dump(mode="json"))
        if assignment.skill == "fastweb.portal.read":
            return self._read_fastweb(plan.model_dump(mode="json"))
        if assignment.skill == "whatsapp.read":
            return self._read_whatsapp(text, assignment.domain, plan.model_dump(mode="json"))
        if assignment.skill in {"whatsapp.compose", "whatsapp.reply"}:
            return self._compose_whatsapp(
                text, plan.model_dump(mode="json"),
                target=str(assignment.arguments.get("target") or ""),
                reply=assignment.skill == "whatsapp.reply",
            )
        if assignment.domain == "email":
            return self._compose_email(text, plan.model_dump(mode="json"))
        if assignment.domain == "home":
            return self._handle_home(text, plan.model_dump(mode="json"))
        return self._result(
            "planned" if not plan.requires_clarification else "clarification_required",
            "Piano validato." if not plan.requires_clarification else "Serve specificare obiettivo o dominio.",
            plan=plan.model_dump(mode="json"),
        )

    def _search_email(self, text: str, plan: dict[str, Any]) -> UnifiedAssistantResult:
        if self.email_search is None:
            return self._result(
                "unavailable", "Connettore Gmail non disponibile; nessuna ricerca eseguita.",
                plan=plan, tools_executed=False, search_status="CONNECTOR_UNAVAILABLE",
            )
        memory_trace: dict[str, Any] = {
            "active_domain": "tiremm",
            "allowed_namespaces": ["tiremm", "general_preferences"],
            "retrieved_memory": [],
            "excluded_memory": [],
        }
        if self.memory_router is not None:
            retrieval = self.memory_router.retrieve(
                self.planner.registry.domain("tiremm"),
                requested_namespaces=("tiremm",),
                facts_only=True,
                limit=8,
            )
            memory_trace = {
                "active_domain": retrieval.trace.active_domain,
                "allowed_namespaces": list(retrieval.trace.allowed_namespaces),
                "retrieved_memory": list(retrieval.trace.retrieved_items),
                "excluded_memory": [item.model_dump(mode="json") for item in retrieval.trace.excluded_items],
            }
        result = self.email_search.search(text)
        self.conversation.remember(
            intent="email.search", domain="tiremm", entities=(result.intent.organization,)
        )
        verification = {
            "FOUND": "evidence_found",
            "NOT_FOUND_IN_SEARCHED_SCOPE": "searched_scope_empty",
            "SEARCH_INCOMPLETE": "searched_scope_incomplete",
            "CONNECTOR_UNAVAILABLE": "connector_unavailable",
        }[result.status]
        self._audit(
            domain="tiremm", intent="email.search", skill="email.search",
            policy=PolicyClass.READ, target=result.intent.organization,
            verification=verification, tool_result=result.status,
            tool="google_workspace.gmail.read_only",
            memory_namespaces=("tiremm",),
        )
        public_evidence = [{
            "message_id": item.message_id,
            "thread_id": item.thread_id,
            "sender": item.sender,
            "subject": item.subject,
            "date": item.date,
            "matched_terms": list(item.matched_terms),
            "provenance_ref": item.provenance_ref,
            "content_role": item.content_role,
        } for item in result.evidence]
        status = (
            "unavailable" if result.status == "CONNECTOR_UNAVAILABLE"
            else "partial" if result.status == "SEARCH_INCOMPLETE"
            else "completed"
        )
        return self._result(
            status,
            result.response,
            plan=plan,
            interaction_class="TOOL_BACKED_READ",
            selected_skill="email.search",
            tool_selected="google_workspace.gmail.read_only",
            policy=PolicyClass.READ.value,
            tools_executed=bool(result.connector_operations),
            connector_operations=list(result.connector_operations),
            search_status=result.status,
            evidence_status=result.evidence_status,
            search_complete=result.search_complete,
            searched_scope=result.searched_scope,
            searched_queries=list(result.searched_queries),
            failed_queries=list(result.failed_queries),
            query_progress=[item.model_dump(mode="json") for item in result.query_progress],
            incomplete_reasons=list(result.incomplete_reasons),
            pages_read=result.pages_read,
            messages_seen=result.messages_seen,
            messages_hydrated=result.messages_hydrated,
            dedup_count=result.dedup_count,
            cap_reached=result.cap_reached,
            evidence=public_evidence,
            provenance=[item.provenance_ref for item in result.evidence],
            synthesis_fallback=result.synthesis_fallback,
            memory_trace=memory_trace,
            side_effects=0,
        )

    def _read_fastweb(self, plan: dict[str, Any]) -> UnifiedAssistantResult:
        if self.fastweb_portal is None:
            return self._result(
                "unavailable", "MyFastPage non disponibile; nessun dato letto.",
                plan=plan, tools_executed=False, side_effects=0,
            )
        result = self.fastweb_portal.read()
        status = "completed" if result.status in {"FOUND", "UNKNOWN"} else "unavailable"
        self._audit(
            domain="tiremm", intent="fastweb.portal.read", skill="fastweb.portal.read",
            policy=PolicyClass.READ, target="myfastweb",
            verification=result.status, tool_result=result.status,
            tool="fastweb.portal.read_only", memory_namespaces=("tiremm",),
        )
        return self._result(
            status, result.response, plan=plan,
            interaction_class="TOOL_BACKED_READ",
            selected_skill="fastweb.portal.read",
            tool_selected="fastweb.portal.read_only",
            policy=PolicyClass.READ.value,
            tools_executed=bool(result.read_operations),
            portal=result.model_dump(mode="json"),
            provenance=list(result.provenance),
            memory_trace=self._memory_trace("tiremm"),
            side_effects=0,
        )

    def _read_whatsapp(self, text: str, domain: str, plan: dict[str, Any]) -> UnifiedAssistantResult:
        if self.whatsapp_read is None:
            return self._result(
                "unavailable", "WhatsApp Web READ non disponibile.",
                plan=plan, tools_executed=False, side_effects=0,
            )
        domain_spec = self.planner.registry.domain(domain)
        namespaces = tuple(item.value for item in domain_spec.allowed_memory_namespaces)
        result = self.whatsapp_read.read(text, allowed_namespaces=namespaces)
        status = "completed" if result.status == "FOUND" else (
            "clarification_required" if result.status in {"SCOPE_UNRESOLVED", "CHAT_NOT_OPEN"}
            else "unavailable"
        )
        self._audit(
            domain=domain, intent="whatsapp.read", skill="whatsapp.read",
            policy=PolicyClass.READ, target=result.chat_ref or "unresolved",
            verification=result.status, tool_result=result.status,
            tool="whatsapp.web.mcp", memory_namespaces=((result.namespace,) if result.namespace else ()),
        )
        return self._result(
            status, result.response, plan=plan,
            interaction_class="TOOL_BACKED_READ",
            selected_skill="whatsapp.read", tool_selected="whatsapp.web.mcp",
            policy=PolicyClass.READ.value,
            tools_executed=bool(result.read_operations),
            whatsapp=result.model_dump(mode="json"),
            provenance=list(result.provenance),
            memory_trace=self._memory_trace(domain),
            persistent_memory_writes=0,
            side_effects=0,
        )

    def _memory_trace(self, domain: str) -> dict[str, Any]:
        spec = self.planner.registry.domain(domain)
        allowed = [item.value for item in spec.allowed_memory_namespaces]
        if self.memory_router is None:
            return {
                "active_domain": domain, "allowed_namespaces": allowed,
                "retrieved_memory": [], "excluded_memory": [],
            }
        requested = tuple(value for value in allowed if value != "general_preferences")
        retrieval = self.memory_router.retrieve(
            spec, requested_namespaces=requested, facts_only=True, limit=8,
        )
        return {
            "active_domain": retrieval.trace.active_domain,
            "allowed_namespaces": list(retrieval.trace.allowed_namespaces),
            "retrieved_memory": list(retrieval.trace.retrieved_items),
            "excluded_memory": [item.model_dump(mode="json") for item in retrieval.trace.excluded_items],
        }

    def _compose_whatsapp(
        self, text: str, plan: dict[str, Any], *, target: str, reply: bool,
        structured_artifacts: tuple[Mapping[str, Any], ...] = (),
    ) -> UnifiedAssistantResult:
        if self.whatsapp_compose is None:
            return self._result("unavailable", "WhatsApp compose adapter unavailable.", plan=plan)
        outcome = self.whatsapp_compose.prepare(
            self.conversation, instruction=text, target=target, reply=reply,
            structured_artifacts=structured_artifacts,
        )
        data: dict[str, Any] = {
            "plan": plan, "selected_skill": "whatsapp.reply" if reply else "whatsapp.compose",
            "tool_selected": "whatsapp.web.mcp", "policy": PolicyClass.CONFIRM_WRITE.value,
            "send_calls": outcome.send_calls, "risk": outcome.risk,
            "ds4_invoked": outcome.ds4_invoked,
            "memory_namespaces": ["tiremm"], "side_effects": 0,
        }
        if outcome.pending is not None:
            data.update({
                "pending_id": outcome.pending.pending_id,
                "draft_version": outcome.pending.version,
                "draft_digest": outcome.pending.payload_digest,
            })
        if outcome.candidates:
            data["candidates"] = list(outcome.candidates)
        return self._result(outcome.status, outcome.message, **data)

    def _revise_whatsapp(self, instruction: str) -> UnifiedAssistantResult:
        if self.whatsapp_compose is None:
            return self._result("unavailable", "WhatsApp compose adapter unavailable.")
        outcome = self.whatsapp_compose.revise(self.conversation, instruction=instruction)
        data: dict[str, Any] = {
            "selected_skill": "whatsapp.compose", "send_calls": 0,
            "previous_approval_invalidated": outcome.pending is not None,
            "risk": outcome.risk, "ds4_invoked": outcome.ds4_invoked,
        }
        if outcome.pending is not None:
            data.update({
                "pending_id": outcome.pending.pending_id,
                "draft_version": outcome.pending.version,
                "draft_digest": outcome.pending.payload_digest,
            })
        return self._result(outcome.status, outcome.message, **data)

    def _compose_email(
        self,
        text: str,
        plan: dict[str, Any],
        *,
        structured_artifacts: tuple[Mapping[str, Any], ...] = (),
    ) -> UnifiedAssistantResult:
        if not all((self.email_memory, self.email_pipeline, self.recipient_resolver)):
            return self._result("unavailable", "Email assistant adapter unavailable.", plan=plan)
        reply_requested = _is_reply_request(text)
        exact_address = _verified_recipient_address(text) if reply_requested else None
        exact_message_id = _explicit_source_message_id(text) if reply_requested else None

        label = _recipient_label(text) or exact_address
        if not label:
            return self._result(
                "clarification_required",
                "Destinatario non risolto.",
                plan=plan,
            )

        if reply_requested and exact_address and exact_message_id:
            exact_resolver = getattr(
                self.recipient_resolver,
                "resolve_exact_reply",
                None,
            )
            if exact_resolver is None:
                return self._result(
                    "unavailable",
                    "Resolver Gmail exact-reply non disponibile.",
                    plan=plan,
                )

            recipient = exact_resolver(
                exact_address,
                exact_message_id,
            )

            if recipient and recipient.get("status") == "mismatch":
                return self._result(
                    "blocked",
                    "Messaggio Gmail sorgente non compatibile con il destinatario verificato.",
                    plan=plan,
                    reason=recipient.get("reason"),
                )

            if not recipient:
                return self._result(
                    "clarification_required",
                    "Messaggio Gmail sorgente esatto non verificato.",
                    plan=plan,
                )
        else:
            recipient = self.recipient_resolver.resolve(label)

        if recipient and recipient.get("status") == "ambiguous":
            return self._result(
                "clarification_required",
                "Più destinatari compatibili: specifica quale.",
                plan=plan,
                candidates=list(recipient.get("candidates") or []),
            )

        if not recipient or not recipient.get("address"):
            return self._result(
                "clarification_required",
                "Destinatario non risolto.",
                plan=plan,
            )

        resolved_source = (
            recipient.get("source_email")
            if isinstance(recipient.get("source_email"), Mapping)
            else None
        )
        resolved_thread = (
            recipient.get("thread_context")
            if isinstance(recipient.get("thread_context"), (list, tuple))
            else ()
        )

        if not resolved_thread and isinstance(resolved_source, Mapping):
            resolved_thread = (
                resolved_source.get("thread_context")
                if isinstance(resolved_source.get("thread_context"), (list, tuple))
                else ()
            )
        if reply_requested and not str((resolved_source or {}).get("message_id") or ""):
            return self._result(
                "clarification_required",
                "Thread di risposta non risolto; nessuna nuova email creata automaticamente.",
                plan=plan,
            )
        resolved_subject = str(recipient.get("subject") or "") if reply_requested else ""
        if reply_requested and not resolved_subject and isinstance(resolved_source, Mapping):
            resolved_subject = str(resolved_source.get("subject") or "")
        if not resolved_subject:
            resolved_subject = "Comunicazione Tiremm Innanz APS"
        working = self.email_memory.build(
            objective=text,
            recipient=recipient.get("name") or label,
            subject=resolved_subject,
            source_email=resolved_source,
            thread_context=resolved_thread,
            structured_artifacts=structured_artifacts,
        )
        outcome = self.email_pipeline.compose(working)
        if outcome.hard_guard != "passed" or outcome.final_validator != "passed":
            return self._result(
                "blocked", "Bozza bloccata dai validator.",
                hard_guard=outcome.hard_guard, final_validator=outcome.final_validator,
            )
        payload = pending_email_payload(
            recipient=recipient["address"],
            subject=resolved_subject,
            body=outcome.body,
            working=working,
            risk=outcome.risk,
            validation_state=outcome.final_validator,
            reply_mode=reply_requested,
        )
        display = _email_display(
            recipient.get("name") or label, outcome.body, resolved_subject
        )
        pending = self.conversation.stage(
            domain="email",
            action=str(payload["approval_action"]),
            policy=PolicyClass.CONFIRM_WRITE,
            payload=payload,
            displayed_text=display,
        )
        self._email_working[pending.pending_id] = working
        self.conversation.remember(intent="email.compose", domain="email", entities=(recipient["address"],))
        self._audit(
            domain="email", intent="email.compose", skill="email.compose",
            policy=PolicyClass.CONFIRM_WRITE, target=recipient["address"],
            verification=outcome.final_validator, pending=pending,
        )
        return self._result(
            "draft_pending_approval", display,
            pending_id=pending.pending_id,
            draft_version=pending.version,
            draft_digest=pending.payload_digest,
            risk=outcome.risk,
            ds4_invoked=outcome.ds4_invoked,
            send_calls=0,
        )

    def _revise_email(self, instruction: str) -> UnifiedAssistantResult:
        pending = self.conversation.state.pending.email
        assert pending is not None
        working = self._email_working.get(pending.pending_id)
        if working is None and self.email_memory is not None:
            seed = pending.payload.get("working_seed")
            if isinstance(seed, Mapping):
                source = seed.get("source_email") if isinstance(seed.get("source_email"), Mapping) else None
                thread = seed.get("thread_context") if isinstance(seed.get("thread_context"), (list, tuple)) else ()
                working = self.email_memory.build(
                    objective=str(seed.get("objective") or "Revise requested draft."),
                    recipient=str(pending.payload.get("recipient") or "recipient"),
                    subject=str(pending.payload.get("subject") or ""),
                    source_email=source,
                    thread_context=thread,
                )
        if working is None or self.email_pipeline is None:
            return self._result("unavailable", "Email draft context unavailable.")
        outcome = self.email_pipeline.revise(working, str(pending.payload.get("body") or ""), instruction)
        if outcome.hard_guard != "passed" or outcome.final_validator != "passed":
            return self._result("blocked", "Modifica bloccata dai validator.")
        display = _email_display(
            str(pending.payload.get("recipient") or "destinatario"),
            outcome.body,
            str(pending.payload.get("subject") or ""),
        )
        revised = self.conversation.revise_email(
            body=outcome.body,
            displayed_text=display,
            risk=outcome.risk,
            validation_state=outcome.final_validator,
        )
        self._email_working.pop(pending.pending_id, None)
        self._email_working[revised.pending_id] = working
        return self._result(
            "draft_pending_approval", display,
            pending_id=revised.pending_id,
            draft_version=revised.version,
            draft_digest=revised.payload_digest,
            previous_approval_invalidated=True,
            send_calls=0,
        )

    def _handle_home(self, text: str, plan: dict[str, Any]) -> UnifiedAssistantResult:
        if self.home_workflow is None:
            return self._result("unavailable", "Home Assistant adapter unavailable.", plan=plan)
        try:
            command = self.home_parser.parse(text, last_entity=self.conversation.last_entity("home"))
            if command.operation == "read":
                if not (self.flags.home_assistant_read_live or self.flags.home_assistant_live):
                    return self._result("disabled", "Home read live disabled.", plan=plan)
            elif not self.flags.home_assistant_live:
                return self._result("disabled", "Home write live disabled.", plan=plan, write_calls=0)
            prepared = self.home_workflow.prepare(command)
        except ValueError as exc:
            return self._result("denied", str(exc), write_calls=0)
        if prepared.policy is PolicyClass.DENY:
            result = self.home_workflow.execute(prepared)
            return self._result(result.status, result.reason, home=result.model_dump(mode="json"))
        if prepared.policy in {PolicyClass.CONFIRM_WRITE, PolicyClass.PROTECTED}:
            payload = {
                "operation": command.operation,
                "targets": [item.entity_id for item in prepared.entities],
                "value": command.value,
            }
            display = "Confermi " + command.operation + " su " + ", ".join(payload["targets"]) + "?"
            pending = self.conversation.stage(
                domain="home", action=command.operation, policy=prepared.policy,
                payload=payload, displayed_text=display,
            )
            self._home_prepared[pending.pending_id] = prepared
            self.conversation.remember(
                intent=f"home.{command.operation}", domain="home",
                entities=tuple(item.entity_id for item in prepared.entities),
            )
            return self._result(
                "confirmation_required" if prepared.policy is PolicyClass.CONFIRM_WRITE else "protected_approval_required",
                display,
                pending_id=pending.pending_id,
                policy=prepared.policy.value,
                write_calls=0,
            )
        result = self.home_workflow.execute(prepared)
        self.conversation.remember(
            intent=f"home.{command.operation}", domain="home",
            entities=tuple(item.entity_id for item in prepared.entities),
        )
        self._audit(
            domain="home", intent=f"home.{command.operation}", skill="home.control",
            policy=prepared.policy, target=",".join(result.targets),
            verification=result.reason,
        )
        return self._result(result.status, _home_message(result, prepared), home=result.model_dump(mode="json"))

    def _execute_pending(self, pending: PendingAction) -> UnifiedAssistantResult:
        if not payload_matches(pending):
            return self._result("blocked", "Pending payload hash mismatch.")
        if pending.domain == "email":
            if not self.flags.email_assistant_live:
                return self._result("disabled", "Email assistant live disabled.", send_calls=0)
            if not approval_matches(pending):
                return self._result("approval_required", "Approvazione hash-bound mancante.", send_calls=0)
            if self.approval_executor is None:
                return self._result("unavailable", "Approval executor unavailable.", send_calls=0)
            result = self.approval_executor.execute(pending)
            self._audit(
                domain="email", intent="email.send", skill="email.compose",
                policy=pending.policy, target=str(pending.payload.get("recipient") or ""),
                verification=str(result.get("status") or "failed"), pending=pending,
                tool_result=str(result.get("status") or "failed"),
            )
            if result.get("status") == "executed":
                self.conversation.clear("email")
                return self._result("executed", "Email inviata e confermata dal provider.", result=result)
            if result.get("status") == "already_executed":
                self.conversation.clear("email")
                return self._result(
                    "already_executed", "Email già inviata; nessun secondo invio.", result=result
                )
            if result.get("status") == "approved_but_send_failed":
                self.conversation.clear("email")
                return self._result(
                    "approved_but_send_failed",
                    "Invio non confermato; retry automatico bloccato per evitare duplicati.",
                    result=result,
                )
            return self._result(
                str(result.get("status") or "failed"), "Email non inviata.", result=result
            )
        if pending.domain == "whatsapp":
            if not self.flags.whatsapp_assistant_live:
                return self._result("disabled", "WhatsApp assistant live disabled.", send_calls=0)
            if not approval_matches(pending) or self.whatsapp_approval_executor is None:
                return self._result("approval_required", "Approvazione WhatsApp esplicita richiesta.", send_calls=0)
            result = self.whatsapp_approval_executor.execute(pending)
            self._audit(
                domain="whatsapp", intent=pending.action, skill="whatsapp.reply" if pending.action == "whatsapp_reply" else "whatsapp.compose",
                policy=pending.policy, target=str(pending.payload.get("chat_id") or ""),
                verification=str(result.get("status") or "failed"), pending=pending,
                tool="whatsapp.web.mcp", tool_result=str(result.get("status") or "failed"),
                memory_namespaces=("tiremm",),
            )
            if result.get("status") == "executed":
                self.conversation.clear("whatsapp")
                return self._result("executed", "Messaggio WhatsApp inviato e verificato.", result=result)
            if result.get("status") == "already_executed":
                self.conversation.clear("whatsapp")
                return self._result("already_executed", "Messaggio WhatsApp già inviato; nessun replay.", result=result)
            if result.get("status") in {"EXECUTION_UNCERTAIN", "SEND_UNVERIFIED"}:
                self.conversation.clear("whatsapp")
                return self._result(
                    str(result.get("status")),
                    "Esito WhatsApp non verificato; retry automatico bloccato.", result=result,
                )
            return self._result(str(result.get("status") or "failed"), "WhatsApp non inviato.", result=result)
        if pending.domain == "home":
            prepared = self._home_prepared.get(pending.pending_id)
            if prepared is None and self.home_workflow is not None:
                try:
                    targets = pending.payload.get("targets") or []
                    if len(targets) == 1:
                        command = self.home_parser.parse(
                            _restore_home_command(pending), last_entity=str(targets[0])
                        )
                        prepared = self.home_workflow.prepare(command)
                except (TypeError, ValueError):
                    prepared = None
            if prepared is None or self.home_workflow is None:
                return self._result("unavailable", "Pending Home action unavailable.", write_calls=0)
            if pending.policy is PolicyClass.PROTECTED and not approval_matches(pending):
                return self._result("approval_required", "Approvazione protetta mancante.", write_calls=0)
            result = self.home_workflow.execute(
                prepared,
                confirmed=pending.policy is PolicyClass.CONFIRM_WRITE,
                protected_approval=pending.policy is PolicyClass.PROTECTED and approval_matches(pending),
            )
            self._audit(
                domain="home", intent=f"home.{pending.action}", skill="home.control",
                policy=pending.policy, target=",".join(result.targets),
                verification=result.reason, pending=pending, tool_result=result.status,
            )
            if result.status in {"verified", "command_sent_unverified"}:
                self.conversation.clear("home")
            return self._result(result.status, _home_message(result, prepared), home=result.model_dump(mode="json"))
        return self._result("denied", "Pending domain cannot be confirmed here.")

    def _audit(
        self,
        *,
        domain: str,
        intent: str,
        skill: str,
        policy: PolicyClass,
        target: str,
        verification: str,
        pending: PendingAction | None = None,
        tool_result: str | None = None,
        tool: str = "existing_adapter",
        memory_namespaces: tuple[str, ...] | None = None,
    ) -> None:
        self.audit_events.append({
            "timestamp": datetime.now(UTC).isoformat(),
            "domain": domain,
            "intent": intent,
            "skill": skill,
            "resolved_arguments": {"target": target},
            "memory_namespaces_used": list(memory_namespaces or (
                ("tiremm", "general_preferences") if domain == "email" else ("home",)
            )),
            "policy": policy.value,
            "tool": tool,
            "target": target,
            "approval_id": pending.approval_ref if pending else None,
            "tool_result": tool_result,
            "draft_version": pending.version if pending and domain == "email" else None,
            "draft_hash": pending.payload_digest if pending and domain == "email" else None,
            "verification_result": verification,
        })

    @staticmethod
    def _result(status: str, message: str, **data: Any) -> UnifiedAssistantResult:
        return UnifiedAssistantResult(status=status, message=message, data=data)


def _verified_recipient_address(text: str) -> str | None:
    match = re.search(
        r"\bdestinatari[ao]\s+verificat[ao]\s*:\s*"
        r"([A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
        text,
        re.I,
    )
    return match.group(1).casefold() if match else None


def _explicit_source_message_id(text: str) -> str | None:
    match = re.search(
        r"\bmessaggio(?:/thread)?\s+gmail\s+sorgente"
        r"(?:\s+atteso)?\s*:\s*([0-9a-f]{16,64})\b",
        text,
        re.I,
    )
    return match.group(1).casefold() if match else None


def _recipient_label(text: str) -> str | None:
    patterns = (
        r"\brispondi\s+alla\s+mail\s+di\s+([\wÀ-ÿ'. -]{1,80}?)(?=\s+(?:del|della|dello|delle|relativ[aoe]|che|per|dicendo)\b|\s*:|$)",
        r"\brispondi\s+a\s+([\wÀ-ÿ'. -]{1,80}?)(?=\s+(?:che|per|dicendo|di|ringraziand|e\s+digli)|\s*:|$)",
        r"\bscrivi\s+(?:una\s+mail\s+)?a\s+([\wÀ-ÿ'. -]{1,80}?)(?=\s+(?:che|per|dicendo|di)|\s*:|$)",
        r"\b(?:mail|email)\s+a\s+([\wÀ-ÿ'. -]{1,80}?)(?=\s+(?:che|per|dicendo|di)|\s*:|$)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return " ".join(match.group(1).split())
    return None


def _email_display(recipient: str, body: str, subject: str = "") -> str:
    subject_line = f"\nOggetto: {subject}\n" if subject else ""
    return f"Bozza per {recipient}:{subject_line}\n{body}\n\nInvio?"


def _is_reply_request(text: str) -> bool:
    return bool(re.match(r"^\s*rispondi\b", text, re.I))


def _is_email_revision(text: str) -> bool:
    return bool(re.search(r"\b(?:rendila|cambiala|cambia|aggiungi|togli|modifica)\b", text, re.I))


def _home_message(result: Any, prepared: HomePreparedAction) -> str:
    if result.status == "read" and len(prepared.entities) == 1:
        entity = prepared.entities[0]
        state = result.after.get(entity.entity_id, {})
        if isinstance(state, Mapping):
            temperature = state.get("current_temperature")
            if temperature is None and entity.device_class == "temperature":
                temperature = state.get("state")
            if temperature is not None:
                return f"{entity.friendly_name}: {temperature} °C."
            return f"{entity.friendly_name}: {state.get('state', 'stato non disponibile')}."
    if result.status == "verified":
        return "Azione verificata da Home Assistant."
    if result.status == "command_sent_unverified":
        return "Comando inviato, ma Home Assistant non conferma ancora il nuovo stato."
    return result.reason


def _restore_home_command(pending: PendingAction) -> str:
    target = str((pending.payload.get("targets") or [""])[0])
    value = pending.payload.get("value")
    templates = {
        "turn_on": f"accendi {target}",
        "turn_off": f"spegni {target}",
        "open_cover": f"apri {target}",
        "close_cover": f"chiudi {target}",
        "set_temperature": f"metti {target} a {value} gradi",
        "adjust_temperature": "abbassala" if float(value or 0) < 0 else "alzala",
    }
    if pending.action not in templates:
        raise ValueError("pending_home_operation_invalid")
    return templates[pending.action]


__all__ = [
    "ApprovalBoundExecutor",
    "EmailPipeline",
    "EmailPipelineResult",
    "EmailSearchService",
    "RecipientResolver",
    "UnifiedAssistantCore",
    "UnifiedAssistantResult",
]
