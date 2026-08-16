from __future__ import annotations

from dataclasses import dataclass
from email.utils import parseaddr
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping, Protocol

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy, scope_digest
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains.email_reply import (
    EmailReplyDomainV1,
    EmailReplyDomainValidationError,
    build_email_reply_domain,
    domain_digest,
    validate_draft_against_domain,
)
from ralfloop_agent.domains.storage import append_jsonl
from ralfloop_agent.local_arch.contracts import CompactRoute
from ralfloop_agent.local_arch.router import LocalRouter
from ralfloop_agent.providers.chat import ChatProviderError, build_chat_provider
from ralfloop_agent.providers.gpu_engine_scheduler import (
    GpuEngineTransitionError,
    TransactionalGpuScheduler,
)
from ralfloop_agent.semantic_judge import (
    JudgeAvailabilityError,
    ReviewRisk,
    SemanticDraftJudge,
    SemanticJudgeConfig,
    assess_email_risk,
    build_semantic_judge,
    run_shadow_email_case,
)
from src.mcp_transport import MCPClientSession, MCPError, MCPProtocolError, UnixMCPTransport


READ_ACTIONS = frozenset({"search", "read", "threads", "getThread", "getAttachment", "viewAttachment"})
WRITE_ACTIONS = frozenset({"reply", "replyAll", "send", "forward"})
MAX_GENERATION_ATTEMPTS = 2
CAPABILITIES = {
    "search": "google_workspace.gmail.search",
    "read": "google_workspace.gmail.read",
    "threads": "google_workspace.gmail.thread",
    "getThread": "google_workspace.gmail.thread",
    "getAttachment": "google_workspace.gmail.attachment",
    "viewAttachment": "google_workspace.gmail.attachment",
    "reply": "google_workspace.gmail.reply",
    "replyAll": "google_workspace.gmail.reply",
    "send": "google_workspace.gmail.send",
    "forward": "google_workspace.gmail.send",
}


class GoogleWorkspaceError(RuntimeError):
    pass


class DraftValidationError(GoogleWorkspaceError):
    """Fail-closed draft rejection carrying a stable machine-readable reason."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class ApprovalRequired(GoogleWorkspaceError):
    pass


@dataclass(frozen=True)
class GmailMessage:
    message_id: str
    thread_id: str
    sender: str
    reply_to: str
    subject: str
    date: str
    body: str


@dataclass(frozen=True)
class DraftGeneration:
    text: str
    provider: str
    model: str
    fallback_used: bool


class ReplyContextProvider:
    """Loads reusable, reviewable organization facts from Ralf-owned knowledge."""

    def __init__(self, path: str | Path | None = None, *, organization: str = "Tiremm Innanz APS") -> None:
        self.path = Path(path or Path(__file__).parents[1] / "config" / "reply_context_profiles.json")
        self.organization = organization

    def organization_context(self) -> dict[str, Any]:
        try:
            profiles = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GoogleWorkspaceError("reply_context_knowledge_unavailable") from exc
        profile = (profiles.get("organizations") or {}).get(self.organization)
        if not isinstance(profile, dict) or not isinstance(profile.get("relevant_facts"), list):
            raise GoogleWorkspaceError("reply_context_organization_not_found")
        signature = profile.get("signature")
        if not isinstance(signature, dict):
            raise GoogleWorkspaceError("reply_context_signature_unavailable")
        return {
            "name": self.organization,
            "relevant_facts": [str(item) for item in profile["relevant_facts"] if str(item).strip()],
            "signature": {
                "required": bool(signature.get("required")),
                "name": str(signature.get("name") or "").strip(),
                "organization": str(signature.get("organization") or "").strip(),
            },
            "source": f"ralf_knowledge:{self.path.name}",
        }


class GoogleWorkspaceGateway:
    """Deterministic policy facade over the generic MCP transport."""

    def __init__(self, session: MCPClientSession, *, account: str) -> None:
        self.session = session
        self.account = account
        self.discovered_tools: tuple[str, ...] = ()
        self._manage_email_fields: frozenset[str] = frozenset()
        self._observed_message_ids: set[str] = set()
        self._observed_thread_ids: set[str] = set()

    def discover(self) -> tuple[str, ...]:
        tools = self.session.list_tools()
        self.discovered_tools = tuple(tool.name for tool in tools)
        if "manage_email" not in self.discovered_tools:
            raise MCPProtocolError("manage_email_not_discovered")
        tool = next(item for item in tools if item.name == "manage_email")
        schema = getattr(tool, "input_schema", None)
        if schema is not None:
            self._manage_email_fields = frozenset((schema.get("properties") or {}).keys())
            required = set(schema.get("required") or [])
            operations = set((((schema.get("properties") or {}).get("operation") or {}).get("enum") or []))
            if not {"operation", "email"} <= required or not set(CAPABILITIES) <= operations:
                raise MCPProtocolError("manage_email_schema_mismatch")
        return self.discovered_tools

    @property
    def search_continuation_argument(self) -> str | None:
        """Return only a continuation argument actually declared by the MCP schema."""

        for name in ("pageToken", "cursor", "offset", "continuation"):
            if name in self._manage_email_fields:
                return name
        return None

    def invoke(self, operation: str, **arguments: Any) -> Mapping[str, Any]:
        if operation not in CAPABILITIES:
            raise GoogleWorkspaceError("unsupported_gmail_action")
        if operation in WRITE_ACTIONS:
            raise ApprovalRequired("gmail_write_requires_bound_human_approval")
        if "manage_email" not in self.discovered_tools:
            raise MCPProtocolError("manage_email_not_validated")
        args = {"operation": operation, "email": self.account, **arguments}
        result = self.session.call_tool("manage_email", args)
        structured = _normalize_manage_email_result(
            operation,
            result,
            requested_message_id=str(arguments.get("messageId") or ""),
            requested_thread_id=str(arguments.get("threadId") or ""),
        )
        self._remember_ids(structured)
        return structured

    def reply_approved(self, store: DomainApprovalStore, request_id: str, current_scope: Mapping[str, Any]) -> Mapping[str, Any]:
        """Compatibility entrypoint using the same CAS/verification path as Unified."""

        return self.execute_approved_email(store, request_id, current_scope)

    def execute_approved_email(
        self,
        store: DomainApprovalStore,
        request_id: str,
        current_scope: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Execute one exact approved send/reply after an atomic one-shot claim."""

        row = verify_approved_email_scope(store, request_id, current_scope)
        scope = row["scope"]
        action = str(scope.get("action") or "")
        if action not in {"send_email", "reply_email"}:
            raise GoogleWorkspaceError("approved_email_action_invalid")
        if str(scope.get("account") or "").casefold() != self.account.casefold():
            raise GoogleWorkspaceError("approved_email_account_mismatch")
        body = str(scope.get("body") or "")
        recipient = str(scope.get("recipient") or "")
        if not body.strip() or not _valid_recipient(recipient):
            raise GoogleWorkspaceError("approved_email_scope_invalid")
        if action == "reply_email":
            message_id = str(scope.get("source_message_id") or "")
            thread_id = str(scope.get("thread_id") or "")
            self.validate_observed(message_id=message_id, thread_id=thread_id)
            arguments = {
                "operation": "reply", "email": self.account,
                "messageId": message_id, "body": body,
            }
        else:
            arguments = {
                "operation": "send", "email": self.account,
                "to": recipient, "subject": str(scope.get("subject") or ""), "body": body,
            }
        idempotency_key = str(scope.get("idempotency_key") or "")
        if idempotency_key:
            for field in ("idempotencyKey", "idempotency_key"):
                if field in self._manage_email_fields:
                    arguments[field] = idempotency_key
                    break
        claim = store.claim_execution(request_id, action=action)
        if not claim.get("claimed"):
            return {
                "status": str(claim.get("status") or "execution_not_claimed"),
                "request_id": request_id,
                "sent": False,
            }
        try:
            raw = self.session.call_tool("manage_email", arguments)
        except Exception:
            failure = {
                "status": "approved_but_send_failed", "request_id": request_id,
                "action": action, "sent": False, "retry_allowed": False,
                "reason": "provider_call_failed_or_outcome_uncertain",
            }
            store.finish_claimed_execution(
                request_id, action=action, success=False, result=failure
            )
            return failure
        confirmation = _email_write_confirmation(raw)
        if not confirmation.get("message_id"):
            failure = {
                "status": "approved_but_send_failed", "request_id": request_id,
                "action": action, "sent": False, "retry_allowed": False,
                "reason": "provider_confirmation_missing",
            }
            store.finish_claimed_execution(
                request_id, action=action, success=False, result=failure
            )
            return failure
        if (
            action == "reply_email" and confirmation.get("thread_id")
            and confirmation["thread_id"] != str(scope.get("thread_id") or "")
        ):
            failure = {
                "status": "approved_but_send_failed", "request_id": request_id,
                "action": action, "sent": False, "retry_allowed": False,
                "reason": "provider_thread_confirmation_mismatch",
            }
            store.finish_claimed_execution(
                request_id, action=action, success=False, result=failure
            )
            return failure
        success = {
            "status": "executed", "request_id": request_id, "action": action,
            "sent": True, "provider_message_id": confirmation["message_id"],
            "thread_id": confirmation.get("thread_id") or str(scope.get("thread_id") or ""),
            "recipient": recipient,
            "approved_hash": str(scope.get("artifact_sha256") or ""),
            "approved_version": int(scope.get("draft_version") or 1),
            "idempotency_key": idempotency_key,
            "provider_confirmed_at": int(time.time()),
        }
        completed = store.finish_claimed_execution(
            request_id, action=action, success=True, result=success
        )
        if completed.get("status") != "consumed":
            return {
                "status": "approved_but_send_failed", "request_id": request_id,
                "sent": False, "retry_allowed": False,
                "reason": "execution_finalization_failed",
            }
        return success

    def preflight_approved_email(self, scope: Mapping[str, Any]) -> None:
        """Validate real reply source/thread before consuming one-shot OTP."""

        action = str(scope.get("action") or "")
        if action == "send_email":
            return
        if action != "reply_email":
            raise GoogleWorkspaceError("approved_email_action_invalid")
        message_id = str(scope.get("source_message_id") or "")
        thread_id = str(scope.get("thread_id") or "")
        if not message_id or not thread_id:
            raise GoogleWorkspaceError("reply_source_missing")
        detail = self.invoke("read", messageId=message_id)
        thread = self.invoke("getThread", threadId=thread_id)
        _validate_reply_preflight(
            detail, thread, message_id=message_id, thread_id=thread_id,
            account=self.account, recipient=str(scope.get("recipient") or ""),
            subject=str(scope.get("subject") or ""),
        )
        self.validate_observed(message_id=message_id, thread_id=thread_id)

    def validate_observed(self, *, message_id: str = "", thread_id: str = "") -> None:
        if message_id and message_id not in self._observed_message_ids:
            raise GoogleWorkspaceError("unobserved_message_id")
        if thread_id and thread_id not in self._observed_thread_ids:
            raise GoogleWorkspaceError("unobserved_thread_id")

    def _remember_ids(self, value: Any) -> None:
        for obj in _walk_dicts(value):
            mid = _first(obj, "messageId", "message_id", "id")
            tid = _first(obj, "threadId", "thread_id")
            if mid:
                self._observed_message_ids.add(mid)
            if tid:
                self._observed_thread_ids.add(tid)


class MagnoliaWorkflow:
    def __init__(
        self,
        gateway: GoogleWorkspaceGateway,
        *,
        approval_store: DomainApprovalStore,
        approval_policy: DomainApprovalPolicy,
        outbox_path: str | Path,
        outbox_state_path: str | Path,
        generator: "ReplyGenerator",
        context_provider: ReplyContextProvider | None = None,
        diagnostic_hook: Callable[[str, str, str | None], None] | None = None,
        semantic_judge: SemanticDraftJudge | None = None,
        semantic_judge_config: SemanticJudgeConfig | None = None,
        risk_classification: ReviewRisk | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = approval_store
        self.policy = approval_policy
        self.outbox_path = Path(outbox_path)
        self.outbox_state_path = Path(outbox_state_path)
        self.generator = generator
        self.context_provider = context_provider or ReplyContextProvider()
        self.diagnostic_hook = diagnostic_hook
        self.semantic_judge_config = semantic_judge_config or SemanticJudgeConfig()
        self.semantic_judge = semantic_judge
        self.risk_classification = risk_classification

    def run(self, request: str, *, route: CompactRoute, route_source: str, wait_delivery_sec: float = 0) -> dict[str, Any]:
        if route.a != "ET" or route.t != "google_workspace.gmail":
            raise GoogleWorkspaceError("validated_google_workspace_route_required")
        discovered = self.gateway.discover()
        search = self.gateway.invoke("search", query="ARCI Magnolia")
        candidates = _candidate_messages(search)
        if not candidates:
            raise GoogleWorkspaceError("magnolia_search_empty")
        try:
            selected = _select_magnolia(candidates)
            message_id = _first(selected, "messageId", "message_id", "id")
            if not message_id:
                raise MCPProtocolError("search_result_missing_message_id")
            detail = self.gateway.invoke("read", messageId=message_id)
        except GoogleWorkspaceError as exc:
            if str(exc) != "magnolia_not_unambiguously_identified":
                raise
            selected, detail = _read_until_magnolia(self.gateway, candidates)
            message_id = _first(selected, "messageId", "message_id", "id")
        message = _parse_message(detail, fallback=selected)
        if not message.thread_id:
            # v4.0.1 drops formatter refs at the MCP boundary. Resolve the ID from
            # the server's read-only thread list, narrowed by the observed subject.
            threads = self.gateway.invoke("threads", query=f'subject:"{_gmail_query_phrase(message.subject)}"', maxResults=10)
            thread_id = _select_thread_id(threads)
            detail = dict(detail)
            detail["threadId"] = thread_id
            message = _parse_message(detail, fallback=selected)
        thread = self.gateway.invoke("getThread", threadId=message.thread_id)
        message = _parse_message(detail, fallback=selected, thread=thread)
        self.gateway.validate_observed(message_id=message.message_id, thread_id=message.thread_id)
        context_packet = _build_reply_context_packet(
            request, message, thread, self.context_provider.organization_context()
        )
        _validate_context_packet(context_packet)
        generated = self.generator.generate(context_packet)
        draft, generator_trace = self._unpack_generation(generated)
        if _env_bool("RALFLOOP_SEMANTIC_JUDGE_SHADOW", False):
            return self._run_semantic_shadow(
                message=message,
                context_packet=context_packet,
                draft=draft,
                generator_trace=generator_trace,
                discovered=discovered,
                route=route,
                route_source=route_source,
            )
        risk_assessment = assess_email_risk(
            context_packet,
            draft,
            classification=self.risk_classification.value if self.risk_classification else None,
        )
        effective_risk = (
            self.risk_classification
            or (ReviewRisk(risk_assessment.level.upper()) if self.semantic_judge_config.semantic_judge_enabled else ReviewRisk.NORMAL)
        )
        semantic_trace = self._empty_semantic_trace()
        semantic_trace.update({
            "semantic_risk_level": effective_risk.value.lower(),
            "semantic_risk_reasons": risk_assessment.reasons,
            "ds4_skipped_reason": None,
        })
        generation_attempts = 1
        rejected_draft_sha256: str | None = None
        initial_reason: str | None = None
        repair_reason: str | None = None
        repair_attempted = False
        if generator_trace["fallback_used"]:
            self._diagnose("initial", draft, None)
            trace = self._trace(context_packet, generator_trace, draft=draft, final_validation="failed",
                                generation_attempts=1, initial_reason=None, repair_attempted=False, repair_reason=None)
            append_jsonl(self._trace_path(), trace)
            raise GoogleWorkspaceError("reply_generator_fallback_forbidden")
        try:
            _validate_draft(draft, context_packet)
        except DraftValidationError as exc:
            initial_reason = exc.reason_code
            rejected_draft_sha256 = hashlib.sha256(draft.encode()).hexdigest()
            self._diagnose("initial", draft, initial_reason)
            if self.semantic_judge_config.should_use(effective_risk):
                semantic_trace["ds4_skipped_reason"] = "hard_guard_block"
                trace = self._trace(
                    context_packet, generator_trace, draft=draft, final_validation="failed",
                    generation_attempts=1, initial_reason=initial_reason,
                    repair_attempted=False, repair_reason=None,
                    rejected_draft_sha256=rejected_draft_sha256,
                    semantic_trace=semantic_trace,
                )
                append_jsonl(self._trace_path(), trace)
                raise
            if initial_reason not in REPAIRABLE_DRAFT_REASONS or not hasattr(self.generator, "generate_repair"):
                trace = self._trace(context_packet, generator_trace, draft=draft, final_validation="failed",
                                    generation_attempts=1, initial_reason=initial_reason,
                                    repair_attempted=False, repair_reason=None,
                                    rejected_draft_sha256=rejected_draft_sha256)
                append_jsonl(self._trace_path(), trace)
                raise
            repair_attempted = True
            generation_attempts = MAX_GENERATION_ATTEMPTS
            repaired = self.generator.generate_repair(context_packet, draft, initial_reason)
            draft, repaired_trace = self._unpack_generation(repaired)
            generator_trace = repaired_trace
            if generator_trace["fallback_used"]:
                self._diagnose("repair", draft, None)
                trace = self._trace(context_packet, generator_trace, draft=draft, final_validation="failed",
                                    generation_attempts=2, initial_reason=initial_reason,
                                    repair_attempted=True, repair_reason=None,
                                    rejected_draft_sha256=rejected_draft_sha256)
                append_jsonl(self._trace_path(), trace)
                raise GoogleWorkspaceError("reply_generator_fallback_forbidden")
            try:
                _validate_draft(draft, context_packet)
            except DraftValidationError as repair_exc:
                repair_reason = repair_exc.reason_code
                self._diagnose("repair", draft, repair_reason)
                trace = self._trace(context_packet, generator_trace, draft=draft, final_validation="failed",
                                    generation_attempts=2, initial_reason=initial_reason,
                                    repair_attempted=True, repair_reason=repair_reason,
                                    rejected_draft_sha256=rejected_draft_sha256)
                append_jsonl(self._trace_path(), trace)
                raise
            self._diagnose("repair", draft, None)
        else:
            self._diagnose("initial", draft, None)
        config = self.semantic_judge_config
        def record_semantic_failure() -> None:
            trace = self._trace(
                context_packet, generator_trace, draft=draft, final_validation="failed",
                generation_attempts=generation_attempts, initial_reason=initial_reason,
                repair_attempted=repair_attempted, repair_reason=repair_reason,
                rejected_draft_sha256=rejected_draft_sha256, semantic_trace=semantic_trace,
            )
            append_jsonl(self._trace_path(), trace)
        if effective_risk is ReviewRisk.HIGH and not config.semantic_judge_enabled:
            semantic_trace["ds4_skipped_reason"] = "judge_disabled"
            record_semantic_failure()
            raise GoogleWorkspaceError("semantic_judge_required_unavailable")
        if config.should_use(effective_risk):
            try:
                judge = self.semantic_judge or build_semantic_judge(config)
            except JudgeAvailabilityError as exc:
                semantic_trace["semantic_judge_status"] = "unavailable"
                record_semantic_failure()
                raise GoogleWorkspaceError("semantic_judge_unavailable") from exc
            semantic_trace.update({
                "semantic_judge_provider": judge.provider,
                "semantic_judge_model": judge.model,
                "semantic_judge_used": True,
            })
            try:
                semantic_result = judge.review(context_packet, draft)
            except TimeoutError as exc:
                semantic_trace.update({"semantic_judge_status": "timeout", "semantic_judge_timeout": True})
                record_semantic_failure()
                raise GoogleWorkspaceError("semantic_judge_timeout") from exc
            except JudgeAvailabilityError as exc:
                semantic_trace["semantic_judge_status"] = "unavailable"
                record_semantic_failure()
                raise GoogleWorkspaceError("semantic_judge_unavailable") from exc
            except ValueError as exc:
                semantic_trace["semantic_judge_status"] = "invalid_output"
                record_semantic_failure()
                raise GoogleWorkspaceError("semantic_judge_invalid_output") from exc
            else:
                review = semantic_result.review
                semantic_trace.update({
                    "semantic_judge_status": review.verdict,
                    "semantic_judge_verdict": review.verdict,
                    "semantic_judge_latency_ms": semantic_result.latency_ms,
                    "semantic_judge_issues": [item.model_dump(mode="json") for item in review.issues],
                    "semantic_judge_summary": review.summary,
                    "semantic_judge_timeout": False,
                })
                if review.verdict == "repair":
                    if repair_attempted or not hasattr(self.generator, "generate_repair"):
                        record_semantic_failure()
                        raise GoogleWorkspaceError("semantic_repair_without_repair_budget")
                    repair_attempted = True
                    generation_attempts = MAX_GENERATION_ATTEMPTS
                    rejected_draft_sha256 = hashlib.sha256(draft.encode()).hexdigest()
                    repaired = self.generator.generate_repair(
                        context_packet, draft, "semantic_judge_repair",
                        semantic_issues=[item.model_dump(mode="json") for item in review.issues],
                    )
                    draft, generator_trace = self._unpack_generation(repaired)
                    if generator_trace["fallback_used"]:
                        record_semantic_failure()
                        raise GoogleWorkspaceError("reply_generator_fallback_forbidden")
                    try:
                        _validate_draft(draft, context_packet)
                    except DraftValidationError as repair_exc:
                        repair_reason = repair_exc.reason_code
                        record_semantic_failure()
                        raise
                    self._diagnose("repair", draft, None)
        else:
            semantic_trace["ds4_skipped_reason"] = f"risk_{effective_risk.value.lower()}"
        try:
            _validate_final_draft(draft, context_packet)
        except DraftValidationError as final_exc:
            repair_reason = final_exc.reason_code
            record_semantic_failure()
            raise
        trace = self._trace(context_packet, generator_trace, draft=draft, final_validation="passed",
                            generation_attempts=generation_attempts, initial_reason=initial_reason,
                            repair_attempted=repair_attempted, repair_reason=repair_reason,
                            rejected_draft_sha256=rejected_draft_sha256, semantic_trace=semantic_trace)
        append_jsonl(self._trace_path(), trace)
        scope = _email_scope(message, draft, self.gateway.account, trace=trace)
        created = self.store.create_request(
            action="reply_email", bando_id="google_workspace.gmail", version="1",
            scope=scope, requested_by="magnolia_workflow",
        )
        if created.get("status") != "pending":
            raise GoogleWorkspaceError(f"approval_not_created:{created.get('status')}")
        approval = created["request"]
        telegram_text = _telegram_draft(message, draft, approval)
        append_jsonl(self.outbox_path, {
            "status": "queued", "request_id": approval["request_id"],
            "api_url": self.policy.api_url, "message": telegram_text,
        })
        delivery = self.wait_for_delivery(approval["request_id"], wait_delivery_sec)
        return {
            "status": "pending_approval",
            "route": {**route.as_dict(), "source": route_source, "validated": True},
            "discovered_tools": list(discovered),
            "mail": message.__dict__,
            "draft": draft,
            "context_packet": context_packet,
            "trace": trace,
            "approval": approval,
            "hash_binding": scope["artifact_sha256"],
            "telegram": delivery,
            "email_sent": False,
        }

    def _unpack_generation(self, generated: DraftGeneration | str) -> tuple[str, dict[str, Any]]:
        if isinstance(generated, DraftGeneration):
            return generated.text, {
                "generator_provider": generated.provider,
                "generator_model": generated.model,
                "fallback_used": generated.fallback_used,
            }
        return str(generated), {
            "generator_provider": str(getattr(self.generator, "provider_name", "test")),
            "generator_model": str(getattr(self.generator, "model_name", "test")),
            "fallback_used": bool(getattr(self.generator, "fallback_used", False)),
        }

    def _diagnose(self, attempt: str, draft: str, reason: str | None) -> None:
        if self.diagnostic_hook is not None:
            self.diagnostic_hook(attempt, draft, reason)

    def _trace_path(self) -> Path:
        return Path(str(self.outbox_path) + ".workflow-trace.jsonl")

    @staticmethod
    def _trace(
        context_packet: Mapping[str, Any], generator_trace: Mapping[str, Any], *, draft: str,
        final_validation: str, generation_attempts: int, initial_reason: str | None,
        repair_attempted: bool, repair_reason: str | None,
        rejected_draft_sha256: str | None = None,
        semantic_trace: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        source = context_packet["source_email"]
        return {
            "context_source_email": bool(source.get("body")),
            "context_thread_loaded": isinstance(source.get("thread_context"), list),
            "organization_context_source": context_packet["organization_context"]["source"],
            "user_intent_present": bool(context_packet["user_intent"]),
            "email_reply_domain_schema": context_packet["email_reply_domain_v1"]["schema_version"],
            "email_reply_domain_sha256": context_packet["email_reply_domain_sha256"],
            **generator_trace,
            "generation_attempts": generation_attempts,
            "initial_validation_reason": initial_reason,
            "repair_attempted": repair_attempted,
            "repair_validation_reason": repair_reason,
            "final_validation": final_validation,
            "draft_validation": final_validation,
            "draft_validation_reason": (repair_reason or initial_reason) if final_validation == "failed" else None,
            "draft_sha256": hashlib.sha256(draft.encode()).hexdigest(),
            "rejected_draft_sha256": rejected_draft_sha256,
            **(semantic_trace or MagnoliaWorkflow._empty_semantic_trace()),
        }

    @staticmethod
    def _empty_semantic_trace() -> dict[str, Any]:
        return {
            "semantic_judge_provider": None,
            "semantic_judge_model": None,
            "semantic_judge_status": "disabled",
            "semantic_judge_verdict": None,
            "semantic_judge_latency_ms": None,
            "semantic_judge_issues": [],
            "semantic_judge_summary": "",
            "semantic_judge_timeout": False,
            "semantic_judge_used": False,
            "semantic_risk_level": None,
            "semantic_risk_reasons": [],
            "ds4_skipped_reason": None,
        }

    def _run_semantic_shadow(
        self,
        *,
        message: GmailMessage,
        context_packet: Mapping[str, Any],
        draft: str,
        generator_trace: Mapping[str, Any],
        discovered: tuple[str, ...],
        route: CompactRoute,
        route_source: str,
    ) -> dict[str, Any]:
        def hard_guard(body: str, packet: Mapping[str, Any]) -> None:
            if generator_trace["fallback_used"]:
                raise GoogleWorkspaceError("reply_generator_fallback_forbidden")
            _validate_draft(body, packet)

        def repair(packet: Mapping[str, Any], rejected: str, issues: list[Mapping[str, Any]]) -> Any:
            if not hasattr(self.generator, "generate_repair"):
                raise GoogleWorkspaceError("semantic_repair_without_repair_budget")
            return self.generator.generate_repair(
                packet,
                rejected,
                "semantic_judge_repair",
                semantic_issues=issues,
            )

        case_id = "bot-tazzi-" + hashlib.sha256(
            f"{message.message_id}\x00{draft}".encode()
        ).hexdigest()[:16]
        artifact_root = Path(os.getenv(
            "RALFLOOP_SEMANTIC_JUDGE_SHADOW_ARTIFACT_DIR",
            str(Path(__file__).parents[1] / ".ralf_run" / "email-semantic-shadow"),
        ))
        result = run_shadow_email_case(
            case_id=case_id,
            context_packet=context_packet,
            draft=draft,
            artifact_root=artifact_root,
            semantic_judge=self.semantic_judge,
            semantic_judge_config=self.semantic_judge_config,
            repair_callback=repair,
            hard_guard=hard_guard,
            final_validator=_validate_final_draft,
            risk_classification=self.risk_classification.value if self.risk_classification else None,
        )
        return {
            **result,
            "status": "shadow_complete",
            "route": {**route.as_dict(), "source": route_source, "validated": True},
            "discovered_tools": list(discovered),
            "approval": None,
            "email_sent": False,
            "telegram": {"status": "not_created", "message_ids": []},
        }

    def wait_for_delivery(self, request_id: str, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, timeout)
        while True:
            state = _read_json(self.outbox_state_path)
            if request_id in set(map(str, state.get("delivered_request_ids", []))):
                ids = []
                for key, value in (state.get("reply_map") or {}).items():
                    if isinstance(value, dict) and value.get("request_id") == request_id:
                        try:
                            ids.append(int(str(key).rsplit(":", 1)[1]))
                        except (ValueError, IndexError):
                            pass
                return {"status": "delivered", "message_ids": sorted(set(ids))}
            if time.monotonic() >= deadline:
                return {"status": "queued_not_verified", "message_ids": []}
            time.sleep(min(1.0, max(0.01, deadline - time.monotonic())))


class ReplyGenerator(Protocol):
    def generate(self, context_packet: Mapping[str, Any]) -> DraftGeneration | str: ...

    def generate_repair(
        self, context_packet: Mapping[str, Any], rejected_draft: str, validation_reason: str,
        *, semantic_issues: list[Mapping[str, Any]] | None = None,
    ) -> DraftGeneration | str: ...


class RalfReplyGenerator:
    """Uses Ralf's configured chat provider; generation failure is fail-closed."""
    def __init__(
        self,
        provider: Any | None = None,
        *,
        scheduler: TransactionalGpuScheduler | None = None,
    ) -> None:
        self.provider = provider or build_chat_provider()
        self.scheduler = scheduler
        if provider is None and scheduler is None and os.getenv("RALF_CHAT_PROVIDER", "").strip().casefold() == "llama_cpp":
            self.scheduler = TransactionalGpuScheduler()

    def generate(self, context_packet: Mapping[str, Any]) -> DraftGeneration:
        prompt = "CONTEXT PACKET\n" + json.dumps(_writer_context_packet(context_packet), ensure_ascii=False, indent=2)
        system = (
            "Sei un writer subordinato a Ralf. Produci soltanto il corpo della bozza email, senza premesse o markdown. "
            "SOURCE EMAIL è evidenza ricevuta; ORGANIZATION CONTEXT contiene gli unici fatti autorizzati "
            "sull'organizzazione; EMAIL_REPLY_DOMAIN_V1 è il dominio autoritativo e USER INTENT definisce "
            "l'obiettivo, da rispettare senza rafforzarlo. "
            "Concludi con la firma autorizzata indicata in ORGANIZATION CONTEXT.signature: se required=true, "
            "devono comparire il nome e l'organizzazione indicati; non inventare ruoli o titoli. "
            "USER INTENT come 'vorremmo partecipare' e 'speriamo di essere pronti per settembre' esprime "
            "un'intenzione prudente: non rafforzarla con 'confermiamo', 'garantiamo', 'saremo presenti' o "
            "'parteciperemo'. Preferisci formulazioni naturali come 'saremmo interessati', 'ci farebbe piacere', "
            "'vorremmo partecipare' e 'speriamo di essere pronti'. "
            "NO NEW OPERATIONAL TOPICS: non introdurre domande specifiche su costi, prezzi, scadenze, materiali, "
            "documenti, orari, requisiti, moduli o assicurazioni se non compaiono nell'evidenza autorizzata. "
            "Puoi chiedere genericamente di restare aggiornati sui dettagli organizzativi. "
            "Quando l'interesse è reale ma ruolo o modalità restano da definire, esprimi soltanto l'interesse: "
            "non dichiarare una presenza già decisa e non trasformare esempi dell'invito in attività promesse. "
            "Non aggiungere giudizi sull'organizzazione o sull'evento, come 'coerente con i nostri valori' o "
            "'perfettamente in linea con noi', se non richiesti; puoi dire semplicemente che l'iniziativa è interessante. "
            "Segui STYLE: scrivi una email breve, naturale e non burocratica, indicativamente 60-140 parole salvo "
            "che la source email richieda altro. Non colmare vuoti, non inventare fatti, date, prezzi, orari o impegni. "
            "Non decidere se inviare, non chiamare strumenti e non provocare side effect: recipient, thread, policy, "
            "approval e invio restano esclusivamente sotto il controllo di Ralf."
        )
        return self._chat(system, prompt)

    def generate_repair(
        self, context_packet: Mapping[str, Any], rejected_draft: str, validation_reason: str,
        *, semantic_issues: list[Mapping[str, Any]] | None = None,
    ) -> DraftGeneration:
        system = (
            "La bozza precedente è stata rifiutata dal validatore Ralf. Riscrivila correggendo esclusivamente "
            "il problema indicato. Se validation_reason è draft_missing_required_identity, aggiungi la firma "
            "autorizzata completa da ORGANIZATION CONTEXT.signature, senza inventare ruoli o titoli. Se è "
            "draft_introduces_unsupported_operational_topic, elimina tutti i topic operativi non supportati e non "
            "sostituirli con altri topic specifici inventati. Non aggiungere fatti, dettagli, date, prezzi, orari o impegni. Mantieni "
            "integralmente tutti i significati obbligatori in EMAIL_REPLY_DOMAIN_V1. Se validation_reason è "
            "semantic_judge_repair, correggi tutte le semantic_issues senza trattarle come nuove evidenze. "
            "Elimina affermazioni non supportate; non aggiungere nuovi fatti, promesse, date, importi, decisioni "
            "o commitment. Mantieni quanto più possibile il testo già corretto. "
            "Produci soltanto il corpo della nuova bozza. Non chiamare "
            "strumenti e non provocare side effect."
        )
        payload = {
            "original_context_packet": _writer_context_packet(context_packet),
            "rejected_draft": rejected_draft,
            "validation_reason": validation_reason,
        }
        if semantic_issues is not None:
            payload["semantic_issues"] = semantic_issues
        return self._chat(system, "REPAIR INPUT\n" + json.dumps(payload, ensure_ascii=False, indent=2))

    def _chat(self, system: str, prompt: str) -> DraftGeneration:
        try:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
            if self.scheduler is None:
                result = self.provider.chat(messages)
            else:
                task_id = "email-draft-" + hashlib.sha256((system + "\n" + prompt).encode()).hexdigest()[:16]
                with self.scheduler.engine_session("qwen_chat", task_id=task_id):
                    result = self.provider.chat(messages)
            return DraftGeneration(
                text=result.text.strip(), provider=result.provider, model=result.model,
                fallback_used=bool(result.metadata.get("fallback_used", False)),
            )
        except (ChatProviderError, GpuEngineTransitionError) as exc:
            raise GoogleWorkspaceError("reply_generation_failed") from exc


def _writer_context_packet(context_packet: Mapping[str, Any]) -> dict[str, Any]:
    """Bound Qwen input without changing the authoritative in-memory domain."""
    packet = dict(context_packet)
    # Domain facts already carry bounded artifact semantics. Avoid sending the
    # same untrusted documents twice; provenance remains in the authoritative
    # in-memory packet used by validators and audit.
    packet.pop("structured_artifacts", None)
    packet.pop("memory_refs", None)
    domain = packet.get("email_reply_domain_v1")
    if not isinstance(domain, Mapping):
        return packet

    def rows(name: str, fields: tuple[str, ...], maximum: int) -> list[dict[str, Any]]:
        values = domain.get(name)
        result: list[dict[str, Any]] = []
        for item in values[:maximum] if isinstance(values, list) else ():
            if not isinstance(item, Mapping):
                continue
            projected = {
                key: (str(item[key])[:400] if key in {"text", "statement", "name"} else item[key])
                for key in fields if item.get(key) not in (None, "", [])
            }
            if projected:
                result.append(projected)
        return result

    compact: dict[str, Any] = {
        "schema_version": domain.get("schema_version", "email_reply_domain_v1"),
        "required_meanings": rows(
            "required_meanings", ("key", "text", "certainty", "deterministic_validator"), 20,
        ),
        "supported_facts": rows(
            "supported_facts", ("statement", "certainty", "kind", "actor_refs"), 32,
        ),
        "unknown_facts": list(domain.get("unknown_facts") or ())[:32],
        "forbidden_claims_without_evidence": list(domain.get("forbidden_claims_without_evidence") or ())[:32],
        "allowed_actions": list(domain.get("allowed_actions") or ())[:16],
        "allowed_commitments": list(domain.get("allowed_commitments") or ())[:16],
        "forbidden_commitments_without_evidence": list(domain.get("forbidden_commitments_without_evidence") or ())[:16],
        "supported_dates": rows("supported_dates", ("statement", "certainty"), 16),
        "supported_amounts": rows("supported_amounts", ("statement", "certainty"), 16),
        "decisions": rows("decisions", ("statement", "certainty", "actor_refs"), 16),
        "subjects": rows("subjects", ("name", "role"), 20),
        "user_constraints": list(domain.get("user_constraints") or ())[:32],
    }
    packet["email_reply_domain_v1"] = {
        key: value for key, value in compact.items() if value not in (None, "", [])
    }
    return packet


def dispatch_magnolia_request(request: str, router: LocalRouter, workflow: MagnoliaWorkflow, *, wait_delivery_sec: float = 0) -> dict[str, Any]:
    decision = router.classify(request)
    return workflow.run(request, route=decision.route, route_source=decision.source, wait_delivery_sec=wait_delivery_sec)


def build_session_from_env() -> MCPClientSession:
    socket_path = os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
    timeout = float(os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "20"))
    return MCPClientSession(UnixMCPTransport(socket_path), timeout=timeout, client_name="ralf-google-workspace")


def verify_approved_email_scope(store: DomainApprovalStore, request_id: str, current_scope: Mapping[str, Any]) -> Mapping[str, Any]:
    row = store.get_request(request_id)
    if not row or row.get("status") != "approved" or row.get("consumed_at") is not None:
        raise ApprovalRequired("valid_one_shot_approval_required")
    if int(row.get("expires_at") or 0) <= int(time.time()):
        raise ApprovalRequired("approval_expired")
    if scope_digest(dict(current_scope)) != row.get("scope_digest"):
        store.mark_stale(request_id, ["email_artifact_changed"])
        raise ApprovalRequired("approval_stale")
    return row


def _valid_recipient(value: str) -> bool:
    _, address = parseaddr(value)
    return bool(address and "@" in address and not re.search(r"[\r\n]", value))


def _email_write_confirmation(result: Mapping[str, Any]) -> dict[str, str]:
    structured = result.get("structuredContent")
    root = structured if isinstance(structured, Mapping) else result
    if isinstance(root, Mapping) and root.get("ok") is False:
        raise MCPError(str(root.get("error") or root.get("message") or "mcp_tool_reported_failure"))
    candidates = list(_walk_dicts(root))
    message_id = ""
    thread_id = ""
    for item in candidates:
        message_id = message_id or _first(item, "messageId", "message_id")
        thread_id = thread_id or _first(item, "threadId", "thread_id")
    if not message_id and isinstance(root, Mapping):
        refs = root.get("refs")
        if isinstance(refs, Mapping):
            message_id = _first(refs, "messageId", "message_id", "id")
            thread_id = thread_id or _first(refs, "threadId", "thread_id")
    raw = _raw_text(result)
    if not message_id and raw:
        match = re.search(
            r"(?im)^\s*\*{0,2}message\s+id\s*:\*{0,2}\s*([0-9a-f]{16,256})\s*$",
            raw,
        )
        message_id = match.group(1) if match else ""
    if not thread_id and raw:
        match = re.search(r"(?i)\bthread\s*(?:id)?\s*[:#]\s*([0-9a-f]{16})\b", raw)
        thread_id = match.group(1) if match else ""
    return {"message_id": message_id[:256], "thread_id": thread_id[:256]}


def _validate_reply_preflight(
    detail: Mapping[str, Any],
    thread: Mapping[str, Any],
    *,
    message_id: str,
    thread_id: str,
    account: str,
    recipient: str,
    subject: str,
) -> None:
    message = detail.get("message") if isinstance(detail.get("message"), Mapping) else detail
    if not isinstance(message, Mapping):
        raise GoogleWorkspaceError("reply_source_invalid")
    if _first(message, "messageId", "message_id", "id") != message_id:
        raise GoogleWorkspaceError("reply_source_message_mismatch")
    observed_thread = _first(message, "threadId", "thread_id")
    if observed_thread and observed_thread != thread_id:
        raise GoogleWorkspaceError("reply_source_thread_mismatch")
    if _first(thread, "threadId", "thread_id") != thread_id:
        raise GoogleWorkspaceError("reply_thread_mismatch")
    if not _address_present(_first(message, "from", "sender"), recipient):
        raise GoogleWorkspaceError("reply_source_sender_mismatch")
    # A message can be visible in the authenticated mailbox through BCC,
    # forwarding or an alias even when its To header names another address.
    # Mailbox ownership is established by the account-scoped MCP read itself;
    # bind the mutation to the observed message/thread/sender/subject instead.
    if _normalized_header(_first(message, "subject")) != _normalized_header(subject):
        raise GoogleWorkspaceError("reply_source_subject_mismatch")
    thread_messages = thread.get("messages")
    if not isinstance(thread_messages, list):
        raise GoogleWorkspaceError("reply_thread_messages_missing")
    direct = any(
        isinstance(item, Mapping)
        and _first(item, "messageId", "message_id", "id") == message_id
        for item in thread_messages
    )
    semantic = any(
        isinstance(item, Mapping)
        and _address_present(_first(item, "from", "sender"), recipient)
        and _normalized_header(_first(item, "subject")) == _normalized_header(subject)
        and _normalized_header(_first(item, "date", "internalDate"))
        == _normalized_header(_first(message, "date", "internalDate"))
        for item in thread_messages
    )
    if not direct and not semantic:
        raise GoogleWorkspaceError("reply_source_not_in_thread")


def _address_present(value: str, expected: str) -> bool:
    expected_address = parseaddr(expected)[1].casefold()
    return bool(expected_address) and any(
        parseaddr(part.strip())[1].casefold() == expected_address
        for part in value.split(",")
    )


def _normalized_header(value: str) -> str:
    return " ".join(value.split()).casefold()


_GMAIL_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_SEARCH_HEADER_RE = re.compile(r"^## Messages \((\d+)\)$")
_THREADS_HEADER_RE = re.compile(r"^## Threads \((\d+)\)$")
_THREAD_HEADER_RE = re.compile(r"^## Thread \((\d+) messages\)$")
_THREAD_MESSAGE_RE = re.compile(r"^\*\*(.+)\*\* — (.*)$")


def _normalize_manage_email_result(
    operation: str,
    result: Mapping[str, Any],
    *,
    requested_message_id: str = "",
    requested_thread_id: str = "",
) -> Mapping[str, Any]:
    """Normalize only shapes emitted by google-workspace-mcp 4.0.1.

    IDs are accepted from structured output, list-row grammar, or the already
    observed ID used for a read call. Email body text is never scanned for IDs.
    """
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        if structured.get("ok") is False:
            raise MCPError(str(
                structured.get("error") or structured.get("message") or "mcp_tool_reported_failure"
            ))
        normalized = dict(structured)
        raw = _raw_text(result)
        if raw:
            normalized["_raw_text"] = raw
        return normalized
    raw = _raw_text(result)
    if not raw:
        raise MCPProtocolError("manage_email_missing_text_content")
    primary = raw.split("\n\n---\n", 1)[0]
    if operation == "search":
        return _parse_search_markdown(primary, raw)
    if operation == "read":
        return _parse_read_markdown(primary, raw, requested_message_id)
    if operation == "threads":
        return _parse_threads_markdown(primary, raw)
    if operation == "getThread":
        return _parse_thread_markdown(primary, raw, requested_thread_id)
    raise MCPProtocolError(f"manage_email_unstructured_operation:{operation}")


def _raw_text(result: Mapping[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    texts = [str(item.get("text") or "") for item in content
             if isinstance(item, dict) and item.get("type") == "text"]
    return "\n".join(texts)


def _valid_gmail_id(value: str, error: str) -> str:
    value = value.strip()
    if not _GMAIL_ID_RE.fullmatch(value):
        raise MCPProtocolError(error)
    return value


def _parse_search_markdown(primary: str, raw: str) -> Mapping[str, Any]:
    if primary.startswith("No messages found"):
        return {"messages": [], "_raw_text": raw}
    lines = primary.splitlines()
    match = _SEARCH_HEADER_RE.fullmatch(lines[0] if lines else "")
    if not match or len(lines) < 2 or lines[1] != "":
        raise MCPProtocolError("manage_email_malformed_search_markdown")
    messages = []
    for line in lines[2:]:
        # Formatter fields have fixed maximum widths. rsplit protects a subject
        # containing pipes; the ID and sender boundary is anchored from the left.
        first = line.find(" | ")
        last = line.rfind(" | ")
        if first <= 0 or last <= first:
            raise MCPProtocolError("manage_email_malformed_search_row")
        message_id = _valid_gmail_id(line[:first], "search_result_invalid_message_id")
        middle, date = line[first + 3:last], line[last + 3:]
        sender_end = middle.find(" | ")
        if sender_end < 0:
            # Explicit formatter error row: ID is real, metadata is unavailable.
            if middle.startswith("⚠ "):
                messages.append({"messageId": message_id, "error": middle[2:]})
                continue
            raise MCPProtocolError("manage_email_malformed_search_row")
        sender, subject = middle[:sender_end], middle[sender_end + 3:]
        messages.append({"messageId": message_id, "sender": sender, "subject": subject, "date": date})
    if len(messages) != int(match.group(1)):
        raise MCPProtocolError("manage_email_search_count_mismatch")
    return {"messages": messages, "_raw_text": raw}


def _parse_read_markdown(primary: str, raw: str, requested_message_id: str) -> Mapping[str, Any]:
    message_id = _valid_gmail_id(requested_message_id, "read_missing_message_id")
    lines = primary.splitlines()
    if not lines or not lines[0].startswith("## "):
        raise MCPProtocolError("manage_email_malformed_read_markdown")
    subject = lines[0][3:]
    fields: dict[str, str] = {}
    index = 2 if len(lines) > 1 and lines[1] == "" else 1
    allowed = {"From", "To", "Date", "Labels"}
    while index < len(lines):
        match = re.fullmatch(r"\*\*([^*]+):\*\* (.*)", lines[index])
        if not match:
            break
        name, value = match.groups()
        if name not in allowed:
            raise MCPProtocolError("manage_email_unknown_read_metadata")
        fields[name] = value
        index += 1
    if index < len(lines) and lines[index] == "":
        index += 1
    if not fields.get("From") or not subject:
        raise MCPProtocolError("manage_email_read_missing_required_metadata")
    return {"message": {"messageId": message_id, "threadId": "", "from": fields["From"],
        "to": fields.get("To", ""), "subject": subject, "date": fields.get("Date", ""),
        "labels": fields.get("Labels", ""), "body": "\n".join(lines[index:])}, "_raw_text": raw}


def _parse_threads_markdown(primary: str, raw: str) -> Mapping[str, Any]:
    if primary == "No threads found.":
        return {"threads": [], "_raw_text": raw}
    lines = primary.splitlines()
    match = _THREADS_HEADER_RE.fullmatch(lines[0] if lines else "")
    if not match or len(lines) < 2 or lines[1] != "":
        raise MCPProtocolError("manage_email_malformed_threads_markdown")
    rows = []
    for line in lines[2:]:
        boundary = line.find(" | ")
        if boundary <= 0:
            raise MCPProtocolError("manage_email_malformed_thread_row")
        rows.append({"threadId": _valid_gmail_id(line[:boundary], "thread_result_invalid_thread_id"),
                     "snippet": line[boundary + 3:]})
    if len(rows) != int(match.group(1)):
        raise MCPProtocolError("manage_email_thread_count_mismatch")
    return {"threads": rows, "_raw_text": raw}


def _parse_thread_markdown(primary: str, raw: str, requested_thread_id: str) -> Mapping[str, Any]:
    thread_id = _valid_gmail_id(requested_thread_id, "get_thread_missing_thread_id")
    lines = primary.splitlines()
    count = _THREAD_HEADER_RE.fullmatch(lines[0] if lines else "")
    if not count or len(lines) < 2 or lines[1] != "":
        raise MCPProtocolError("manage_email_malformed_get_thread_markdown")
    messages = []
    index = 2
    while index < len(lines):
        if lines[index] == "":
            index += 1
            continue
        header = _THREAD_MESSAGE_RE.fullmatch(lines[index])
        if not header or index + 1 >= len(lines) or not lines[index + 1].startswith("Subject: "):
            raise MCPProtocolError("manage_email_malformed_thread_message")
        sender, date = header.groups()
        subject = lines[index + 1][9:]
        index += 2
        body_lines = []
        while index < len(lines) and not _THREAD_MESSAGE_RE.fullmatch(lines[index]):
            body_lines.append(lines[index]); index += 1
        while body_lines and body_lines[-1] == "":
            body_lines.pop()
        messages.append({"from": sender, "date": date, "subject": subject, "body": "\n".join(body_lines)})
    if len(messages) != int(count.group(1)):
        raise MCPProtocolError("manage_email_get_thread_count_mismatch")
    return {"threadId": thread_id, "messages": messages, "_raw_text": raw}


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _first(value: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        item = value.get(key)
        if item is not None and str(item).strip():
            return str(item).strip()
    return ""


def _candidate_messages(search: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    for key in ("messages", "results", "emails", "data"):
        rows = search.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
        if isinstance(rows, dict):
            nested = _candidate_messages(rows)
            if nested:
                return nested
    return []


def _select_magnolia(rows: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    scored = []
    for index, row in enumerate(rows):
        text = " ".join(str(row.get(key) or "") for key in ("from", "sender", "subject", "snippet", "body")).casefold()
        score = 4 * ("magnolia" in text) + 3 * ("arci" in text) + 2 * ("settembre" in text) + ("partecip" in text)
        date = _first(row, "internalDate", "date", "receivedAt")
        scored.append((score, date, -index, row))
    scored.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    if scored[0][0] < 4:
        raise GoogleWorkspaceError("magnolia_not_unambiguously_identified")
    return scored[0][3]


def _read_until_magnolia(
    gateway: GoogleWorkspaceGateway, rows: list[Mapping[str, Any]]
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """Hydrate search rows until full content supplies Magnolia evidence."""
    for row in rows:
        message_id = _first(row, "messageId", "message_id", "id")
        if not message_id or row.get("error"):
            continue
        detail = gateway.invoke("read", messageId=message_id)
        message = _parse_message(detail, fallback=row)
        text = " ".join((message.sender, message.subject, message.body)).casefold()
        if "magnolia" in text and "arci" in text and any(word in text for word in ("invit", "partecip", "festival")):
            return row, detail
    raise GoogleWorkspaceError("magnolia_not_unambiguously_identified")


def _gmail_query_phrase(value: str) -> str:
    # Gmail query quotes cannot safely contain a literal quote. Refuse instead of
    # broadening the query and risking association with an unrelated thread.
    if not value or '"' in value or "\r" in value or "\n" in value:
        raise MCPProtocolError("unsafe_subject_for_thread_lookup")
    return value


def _select_thread_id(value: Mapping[str, Any]) -> str:
    rows = value.get("threads")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise MCPProtocolError("mail_thread_not_unambiguous")
    return _valid_gmail_id(_first(rows[0], "threadId", "thread_id", "id"), "mail_missing_thread_id")


def _parse_message(detail: Mapping[str, Any], *, fallback: Mapping[str, Any], thread: Mapping[str, Any] | None = None) -> GmailMessage:
    objects = list(_walk_dicts(detail))
    if thread:
        objects += list(_walk_dicts(thread))
    objects.append(dict(fallback))
    def pick(*keys: str) -> str:
        return next((value for obj in objects if (value := _first(obj, *keys))), "")
    sender = pick("from", "sender", "From")
    reply_to = pick("replyTo", "reply_to", "Reply-To") or parseaddr(sender)[1]
    return GmailMessage(
        pick("messageId", "message_id", "id"), pick("threadId", "thread_id"), sender,
        reply_to, pick("subject", "Subject"), pick("date", "internalDate", "receivedAt"),
        pick("body", "text", "plainText", "snippet"),
    )


def _thread_context(thread: Mapping[str, Any], current: GmailMessage) -> list[dict[str, str]]:
    rows = thread.get("messages")
    if not isinstance(rows, list):
        return []
    context = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item = {
            "sender": _first(row, "from", "sender"),
            "subject": _first(row, "subject", "Subject"),
            "date": _first(row, "date", "internalDate"),
            "body": _first(row, "body", "text", "plainText", "snippet"),
        }
        same_current = (
            _first(row, "messageId", "message_id", "id") == current.message_id
            or (item["sender"] == current.sender and item["subject"] == current.subject and item["date"] == current.date)
            or (item["body"] == current.body and not any((item["sender"], item["subject"], item["date"])))
        )
        if not same_current and any(item.values()):
            context.append(item)
    return context


def _user_intent(request: str) -> list[str]:
    """Retain user-authored intent clauses without adding model assumptions."""
    normalized = " ".join(request.split()).strip()
    clauses = re.findall(
        r"(?i)\b(?:vorremmo|vogliamo|desideriamo|possiamo|speriamo|conferma(?:re)?|"
        r"comunica(?:re)?|dire|di')\b.*?(?=(?:[.;]|\bprepara\b|\bmandami\b|$))",
        normalized,
    )
    return [clause.strip(" .") for clause in clauses] or ([normalized] if normalized else [])


def _build_reply_context_packet(
    request: str,
    message: GmailMessage,
    thread: Mapping[str, Any],
    organization_context: Mapping[str, Any],
) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "task": "draft_email_reply",
        "source_email": {
            "sender": message.sender,
            "sender_email": parseaddr(message.sender)[1],
            "subject": message.subject,
            "date": message.date,
            "message_id": message.message_id,
            "thread_id": message.thread_id,
            "body": message.body,
            "thread_context": _thread_context(thread, message),
        },
        "organization_context": dict(organization_context),
        "user_intent": _user_intent(request),
        "reply_constraints": {
            "do_not_claim_confirmed_participation": True,
            "do_not_invent_facts": True,
            "do_not_invent_dates": True,
            "do_not_invent_prices": True,
            "do_not_invent_commitments": True,
            "language": "it",
            "tone": "cordiale e semplice",
            "output": "draft_body_only",
            "side_effects_allowed": False,
        },
        "style": {
            "concise": True,
            "natural": True,
            "avoid_bureaucratic_language": True,
        },
    }
    domain = build_email_reply_domain(packet)
    packet["email_reply_domain_v1"] = domain.model_dump(mode="json")
    packet["email_reply_domain_sha256"] = domain_digest(domain)
    return packet


def _validate_context_packet(context_packet: Mapping[str, Any]) -> None:
    source = context_packet.get("source_email")
    if not isinstance(source, Mapping) or not str(source.get("body") or "").strip():
        raise GoogleWorkspaceError("reply_source_email_missing")
    organization = context_packet.get("organization_context")
    if (
        not isinstance(organization, Mapping)
        or not str(organization.get("source") or "").startswith("ralf_knowledge:")
        or not isinstance(organization.get("relevant_facts"), list)
        or not isinstance(organization.get("signature"), Mapping)
    ):
        raise GoogleWorkspaceError("reply_context_knowledge_unavailable")
    signature = organization["signature"]
    if signature.get("required") and (
        not str(signature.get("name") or "").strip()
        or not str(signature.get("organization") or "").strip()
    ):
        raise GoogleWorkspaceError("reply_context_signature_unavailable")
    try:
        domain = EmailReplyDomainV1.model_validate(context_packet.get("email_reply_domain_v1"))
    except (TypeError, ValueError) as exc:
        raise GoogleWorkspaceError("email_reply_domain_invalid") from exc
    if context_packet.get("email_reply_domain_sha256") != domain_digest(domain):
        raise GoogleWorkspaceError("email_reply_domain_digest_mismatch")


REPAIRABLE_DRAFT_REASONS = frozenset({
    "draft_missing_participation_interest",
    "draft_missing_september_uncertainty",
    "draft_claims_confirmed_participation",
    "draft_missing_required_identity",
    "draft_introduces_unsupported_operational_topic",
    "draft_invalid_shape",
})

_CERTAIN_PARTICIPATION = (
    re.compile(r"\bconfermiamo\b.{0,60}\bpartecipazione\b", re.I),
    re.compile(r"\bsaremo\s+(?:certamente\s+)?presenti\b", re.I),
    re.compile(r"\bparteciperemo\b", re.I),
    re.compile(r"\bgarantiamo\b.{0,60}\b(?:presenza|partecipazione)\b", re.I),
    re.compile(r"\b(?:la\s+)?nostra\s+partecipazione\s+(?:è|e')\s+confermata\b", re.I),
)

_PARTICIPATION_INTEREST = (
    re.compile(r"\b(?:siamo|saremmo)\s+interessat[ei]\s+(?:a\s+)?partecipare\b", re.I),
    re.compile(r"\bci\s+(?:piacerebbe|farebbe\s+piacere)\s+partecipare\b", re.I),
    re.compile(r"\b(?:vorremmo|desidereremmo)\s+partecipare\b", re.I),
    re.compile(r"\bvorrebbe\s+partecipare\b", re.I),
    re.compile(r"\b(?:è|e'|sarebbe)\s+interessat[ao]\s+(?:a\s+)?partecipare\b", re.I),
)

_SEPTEMBER_UNCERTAINTY = (
    re.compile(r"\bsperiamo\s+di\s+(?:riuscire\s+(?:ad?\s+)?)?essere\s+pront[ei]\b.{0,50}\bsettembre\b", re.I),
    re.compile(r"\b(?:confidiamo|speriamo)\s+di\s+riuscire\b.{0,70}\bsettembre\b", re.I),
    re.compile(r"\bse\s+riusciremo\s+(?:ad?\s+)?essere\s+pront[ei]\b.{0,50}\bsettembre\b", re.I),
    re.compile(r"\bsperiamo\b.{0,50}\b(?:pront[ei]|operativ[ei]|riuscire)\b.{0,50}\bsettembre\b", re.I),
)

_OPERATIONAL_TOPICS = {
    "cost": re.compile(r"\b(?:costo|costi|prezzo|prezzi|euro)\b", re.I),
    "deadline": re.compile(r"\b(?:scadenza|scadenze)\b", re.I),
    "materials": re.compile(r"\b(?:materiale|materiali)\b", re.I),
    "documents": re.compile(r"\b(?:documento|documenti)\b", re.I),
    "times": re.compile(r"\b(?:orario|orari)\b", re.I),
    "requirements": re.compile(r"\b(?:requisito|requisiti)\b", re.I),
    "forms": re.compile(r"\b(?:modulo|moduli)\b", re.I),
    "insurance": re.compile(r"\b(?:assicurazione|assicurazioni)\b", re.I),
}


def _validate_draft(body: str, context_packet: Mapping[str, Any] | None = None) -> str:
    folded = " ".join(body.casefold().split())
    if not body.strip() or len(body) > 4000 or "```" in body or "\x00" in body:
        raise DraftValidationError("draft_invalid_shape")
    identity = "tiremm innanz"
    identity_required = True
    if context_packet:
        organization = context_packet.get("organization_context") or {}
        signature = organization.get("signature") or {}
        if isinstance(signature, Mapping):
            identity_required = bool(signature.get("required", True))
            identity = str(signature.get("organization") or identity).casefold().strip()
        else:
            identity = str(organization.get("name") or identity).casefold()
            identity = re.sub(r"\s+(?:aps|ets|odv)$", "", identity).strip()
    if identity_required and identity not in folded:
        raise DraftValidationError("draft_missing_required_identity")
    if any(pattern.search(folded) for pattern in _CERTAIN_PARTICIPATION):
        raise DraftValidationError("draft_claims_confirmed_participation")
    if not any(pattern.search(folded) for pattern in _PARTICIPATION_INTEREST):
        raise DraftValidationError("draft_missing_participation_interest")
    if not any(pattern.search(folded) for pattern in _SEPTEMBER_UNCERTAINTY):
        raise DraftValidationError("draft_missing_september_uncertainty")
    if context_packet:
        evidence = json.dumps(context_packet, ensure_ascii=False).casefold()
        for pattern in _OPERATIONAL_TOPICS.values():
            if pattern.search(folded) and not pattern.search(evidence):
                raise DraftValidationError("draft_introduces_unsupported_operational_topic")
        # New prices, clock times and calendar dates are high-risk commitments.
        risky = re.findall(r"(?:\b\d{1,2}[:.]\d{2}\b|\b\d+(?:[,.]\d+)?\s*(?:€|euro)\b|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b)", folded)
        if any(item not in evidence for item in risky):
            raise DraftValidationError("draft_invents_organizational_detail")
    return "passed"


def _validate_final_draft(body: str, context_packet: Mapping[str, Any]) -> str:
    """Final deterministic authority: legacy hard guards plus explicit domain."""
    _validate_draft(body, context_packet)
    try:
        validate_draft_against_domain(body, context_packet["email_reply_domain_v1"])
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, EmailReplyDomainValidationError):
            raise DraftValidationError(exc.reason_code) from exc
        raise DraftValidationError("draft_domain_invalid") from exc
    return "passed"


def _email_scope(message: GmailMessage, body: str, account: str, *, trace: Mapping[str, Any] | None = None) -> dict[str, Any]:
    artifact = {
        "action": "reply_email", "version": 1, "account": account,
        "source_message_id": message.message_id, "thread_id": message.thread_id,
        "recipient": message.reply_to, "subject": message.subject, "body": body,
    }
    raw = json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {**artifact, "artifact_sha256": hashlib.sha256(raw).hexdigest(), "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
            **({"trace": dict(trace)} if trace else {})}


def _telegram_draft(message: GmailMessage, body: str, approval: Mapping[str, Any]) -> str:
    summary = re.sub(r"\s+", " ", message.body).strip()[:500] or "Contenuto disponibile nella mail originale."
    return (
        "ARCI MAGNOLIA — BOZZA RISPOSTA\n\n"
        f"Da:\n{message.sender}\n\nOggetto:\n{message.subject}\n\nSintesi:\n{summary}\n\n"
        f"Bozza:\n{body}\n\nLa mail NON è stata inviata.\n\nConfermi l'invio?\n\n"
        f"ID approvazione:\n{approval['request_id']}\nRequest: {approval['request_id']}\nDigest: {approval['scope_digest_short']}"
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().casefold() in {"1", "true", "yes", "on"}
