from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping, Protocol

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore

from .conversation import PendingAction, approval_matches, payload_matches


CREATE_ACTION = "mailchimp_campaign_create"
SEND_ACTION = "mailchimp_campaign_send"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _canonical_digest(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha(raw)


def build_mailchimp_campaign_create_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    artifact = {
        "action": CREATE_ACTION,
        "version": 1,
        "draft_id": str(payload.get("draft_id") or ""),
        "draft_version": int(payload.get("draft_version") or 1),
        "payload_digest": str(payload.get("payload_digest") or ""),
        "source_draft_sha256": str(payload.get("source_draft_sha256") or ""),
        "list_id": str(payload.get("list_id") or ""),
        "subject": str(payload.get("subject") or ""),
        "from_name": str(payload.get("from_name") or ""),
        "reply_to": str(payload.get("reply_to") or ""),
        "preheader": str(payload.get("preheader") or ""),
        "body_text": str(payload.get("body_text") or ""),
        "cta_label": str(payload.get("cta_label") or ""),
        "cta_target": str(payload.get("cta_target") or ""),
        "internal_title": str(payload.get("internal_title") or ""),
        "provider_identity": str(payload.get("provider_identity") or "mailchimp.marketing"),
    }
    if not all(artifact[key] for key in (
        "draft_id", "payload_digest", "source_draft_sha256", "list_id", "subject",
        "from_name", "reply_to", "body_text", "provider_identity",
    )):
        raise ValueError("mailchimp_create_scope_incomplete")
    body_sha256 = _sha(artifact["body_text"])
    artifact_sha256 = _canonical_digest({**artifact, "body_sha256": body_sha256})
    return {
        **artifact,
        "body_sha256": body_sha256,
        "artifact_sha256": artifact_sha256,
        "execution_id": "mccreate_" + artifact_sha256[:24],
    }


def build_mailchimp_campaign_send_scope(payload: Mapping[str, Any]) -> dict[str, Any]:
    artifact = {
        "action": SEND_ACTION,
        "version": 1,
        "campaign_id": str(payload.get("campaign_id") or ""),
        "list_id": str(payload.get("list_id") or ""),
        "provider_campaign_sha256": str(payload.get("provider_campaign_sha256") or ""),
        "subject": str(payload.get("subject") or ""),
        "from_name": str(payload.get("from_name") or ""),
        "reply_to": str(payload.get("reply_to") or ""),
        "content_sha256": str(payload.get("content_sha256") or ""),
        "provider_identity": str(payload.get("provider_identity") or "mailchimp.marketing"),
    }
    if not all(artifact.values()):
        raise ValueError("mailchimp_send_scope_incomplete")
    artifact_sha256 = _canonical_digest(artifact)
    return {
        **artifact,
        "artifact_sha256": artifact_sha256,
        "body_sha256": artifact["content_sha256"],
        "execution_id": "mcsend_" + artifact_sha256[:24],
    }


class MailchimpCampaignProvider(Protocol):
    def validate_create(self, scope: Mapping[str, Any]) -> None: ...
    def create_campaign(self, scope: Mapping[str, Any]) -> Mapping[str, Any]: ...
    def read_campaign(self, campaign_id: str) -> Mapping[str, Any]: ...
    def send_campaign(self, campaign_id: str) -> None: ...


def campaign_fingerprint(campaign: Mapping[str, Any]) -> str:
    material = {
        "campaign_id": str(campaign.get("campaign_id") or ""),
        "list_id": str(campaign.get("list_id") or ""),
        "subject": str(campaign.get("subject") or ""),
        "from_name": str(campaign.get("from_name") or ""),
        "reply_to": str(campaign.get("reply_to") or ""),
        "content_sha256": str(campaign.get("content_sha256") or ""),
    }
    return _canonical_digest(material)


class MailchimpCampaignWorkflow:
    """The only mutation boundary: approval CAS plus exact provider readback."""

    def __init__(self, store: DomainApprovalStore, provider: MailchimpCampaignProvider) -> None:
        self.store = store
        self.provider = provider

    def execute_create(self, request_id: str, current_scope: Mapping[str, Any]) -> dict[str, Any]:
        checked = self._approved(request_id, CREATE_ACTION, current_scope)
        if checked is not None:
            return checked
        try:
            self.provider.validate_create(current_scope)
        except Exception:
            return {"status": "SOURCE_UNAVAILABLE", "created": False, "retry_allowed": True}
        claim = self.store.claim_execution(request_id, action=CREATE_ACTION)
        if not claim.get("claimed"):
            return {"status": str(claim.get("status")), "created": False, "retry_allowed": False}
        try:
            created = self.provider.create_campaign(current_scope)
            campaign_id = str(created.get("campaign_id") or "")
            observed = self.provider.read_campaign(campaign_id)
            expected = {
                "campaign_id": campaign_id, "list_id": current_scope["list_id"],
                "subject": current_scope["subject"], "from_name": current_scope["from_name"],
                "reply_to": current_scope["reply_to"],
                "content_sha256": current_scope["body_sha256"], "sent": False,
            }
            if not campaign_id or any(observed.get(k) != v for k, v in expected.items()):
                raise RuntimeError("create_postcondition_mismatch")
        except Exception:
            return self._failed(request_id, CREATE_ACTION, "CREATE_UNCERTAIN")
        result = {
            "status": "executed", "state": "DRAFT", "created": True, "sent": False,
            "campaign_id": campaign_id, "provider_campaign_sha256": campaign_fingerprint(observed),
            "provider_evidence": dict(observed), "retry_allowed": False,
        }
        finalized = self.store.finish_claimed_execution(
            request_id, action=CREATE_ACTION, success=True, result=result,
        )
        if finalized.get("status") != "consumed":
            return {"status": "EXECUTION_UNCERTAIN", "created": True,
                    "campaign_id": campaign_id, "retry_allowed": False}
        return result

    def execute_send(self, request_id: str, current_scope: Mapping[str, Any]) -> dict[str, Any]:
        checked = self._approved(request_id, SEND_ACTION, current_scope)
        if checked is not None:
            return checked
        campaign_id = str(current_scope.get("campaign_id") or "")
        try:
            before = self.provider.read_campaign(campaign_id)
        except Exception:
            return {"status": "SOURCE_UNAVAILABLE", "sent": False, "retry_allowed": True}
        if (
            bool(before.get("sent"))
            or campaign_fingerprint(before) != str(current_scope.get("provider_campaign_sha256"))
            or any(before.get(k) != current_scope.get(k) for k in (
                "campaign_id", "list_id", "subject", "from_name", "reply_to", "content_sha256"
            ))
        ):
            self.store.mark_stale(request_id, ["mailchimp_provider_campaign_changed"])
            return {"status": "DRAFT_CHANGED", "sent": False, "retry_allowed": False}
        claim = self.store.claim_execution(request_id, action=SEND_ACTION)
        if not claim.get("claimed"):
            return {"status": str(claim.get("status")), "sent": False, "retry_allowed": False}
        try:
            self.provider.send_campaign(campaign_id)
            after = self.provider.read_campaign(campaign_id)
            if not bool(after.get("sent")) or campaign_fingerprint(after) != campaign_fingerprint(before):
                raise RuntimeError("send_postcondition_mismatch")
        except Exception:
            return self._failed(request_id, SEND_ACTION, "SEND_UNCERTAIN")
        result = {
            "status": "executed", "state": "SENT", "sent": True,
            "campaign_id": campaign_id, "provider_campaign_sha256": campaign_fingerprint(after),
            "provider_evidence": dict(after), "retry_allowed": False,
        }
        finalized = self.store.finish_claimed_execution(
            request_id, action=SEND_ACTION, success=True, result=result,
        )
        if finalized.get("status") != "consumed":
            return {"status": "EXECUTION_UNCERTAIN", "sent": True,
                    "campaign_id": campaign_id, "retry_allowed": False}
        return result

    def reconcile(self, request_id: str, *, action: str, campaign_id: str) -> dict[str, Any]:
        row = self.store.get_request(request_id)
        if row is None or row.get("action") != action:
            return {"status": "reconciliation_state_denied", "reconciled": False}
        observed = self.provider.read_campaign(campaign_id)
        approved = dict(row.get("scope") or {})
        expected = {
            "list_id": approved.get("list_id"), "subject": approved.get("subject"),
            "from_name": approved.get("from_name"), "reply_to": approved.get("reply_to"),
            "content_sha256": (
                approved.get("body_sha256") if action == CREATE_ACTION
                else approved.get("content_sha256")
            ),
        }
        if (
            any(observed.get(key) != value for key, value in expected.items())
            or (action == CREATE_ACTION and bool(observed.get("sent")))
            or (action == SEND_ACTION and not bool(observed.get("sent")))
            or (
                action == SEND_ACTION
                and campaign_fingerprint(observed) != approved.get("provider_campaign_sha256")
            )
        ):
            return {"status": "provider_state_unconfirmed", "reconciled": False}
        evidence = {
            "campaign_id": campaign_id,
            "provider_campaign_sha256": campaign_fingerprint(observed),
            "provider_state": "sent" if observed.get("sent") else "draft",
        }
        return self.store.reconcile_confirmed_campaign_execution(
            request_id, action=action, evidence=evidence,
        )

    def _approved(self, request_id: str, action: str, scope: Mapping[str, Any]) -> dict[str, Any] | None:
        row = self.store.get_request(request_id)
        if row is None or effective_approval_status(row) != "approved":
            return {"status": "APPROVAL_INVALID", "retry_allowed": False}
        if row.get("action") != action:
            return {"status": "POLICY_DENIED", "retry_allowed": False}
        if str(row.get("scope_digest")) != scope_digest(dict(scope)):
            self.store.mark_stale(request_id, ["mailchimp_artifact_changed"])
            return {"status": "DRAFT_CHANGED", "retry_allowed": False}
        return None

    def _failed(self, request_id: str, action: str, status: str) -> dict[str, Any]:
        result = {"status": status, "sent": False, "created": False, "retry_allowed": False}
        self.store.finish_claimed_execution(request_id, action=action, success=False, result=result)
        return result


class UnifiedMailchimpApprovalCoordinator:
    def __init__(self, store: DomainApprovalStore, *, policy: DomainApprovalPolicy) -> None:
        self.store = store
        self.policy = policy

    def request(self, pending: PendingAction, *, requested_by: str) -> dict[str, Any]:
        if not self.policy.enabled or not self.policy.allowed_user_ids or not self.policy.allowed_chat_ids:
            return {"status": "approval_gate_unavailable"}
        scope = self.scope(pending)
        created = self.store.create_request(
            action=pending.action, bando_id="mailchimp.marketing",
            version=str(pending.version), scope=scope, requested_by=requested_by,
        )
        request = created.get("request") if isinstance(created, Mapping) else None
        if not isinstance(request, Mapping):
            return {"status": str(created.get("status") or "approval_request_failed")}
        return {
            "status": "pending", "request_id": str(request["request_id"]),
            "created_at": int(request["created_at"]),
            "expires_at": int(request["expires_at"]),
            "scope_digest_short": str(request["scope_digest_short"]),
        }

    def approve(self, pending: PendingAction, *, telegram_user_id: int, telegram_chat_id: int,
                telegram_message_id: int, chat_type: str = "private") -> dict[str, Any]:
        row = self.store.get_request(str(pending.approval_ref or ""))
        if not row:
            return {"status": "approval_request_missing"}
        scope = self.scope(pending)
        if row.get("scope_digest") != scope_digest(scope):
            self.store.mark_stale(str(pending.approval_ref), ["mailchimp_artifact_changed"])
            return {"status": "scope_digest_mismatch"}
        return self.store.decide(DomainApprovalDecision(
            request_id=str(pending.approval_ref), decision="approve",
            telegram_user_id=telegram_user_id, telegram_chat_id=telegram_chat_id,
            telegram_message_id=telegram_message_id, chat_type=chat_type,
            idempotency_key=f"mailchimp:{pending.pending_id}:{pending.version}:{telegram_message_id}",
        ), scope_digest_short=str(row.get("scope_digest_short") or ""))

    @staticmethod
    def scope(pending: PendingAction) -> dict[str, Any]:
        if pending.domain != "mailchimp":
            raise ValueError("mailchimp_pending_required")
        payload = {**pending.payload, "draft_id": pending.pending_id,
                   "draft_version": pending.version, "payload_digest": pending.payload_digest}
        return (build_mailchimp_campaign_create_scope(payload) if pending.action == CREATE_ACTION
                else build_mailchimp_campaign_send_scope(payload))


class UnifiedMailchimpApprovalExecutor:
    def __init__(self, workflow_factory: Callable[[], MailchimpCampaignWorkflow]) -> None:
        self.workflow_factory = workflow_factory

    def execute(self, pending: PendingAction) -> dict[str, Any]:
        if not payload_matches(pending) or not approval_matches(pending):
            return {"status": "APPROVAL_REQUIRED", "retry_allowed": False}
        scope = UnifiedMailchimpApprovalCoordinator.scope(pending)
        workflow = self.workflow_factory()
        if pending.action == CREATE_ACTION:
            return workflow.execute_create(str(pending.approval_ref), scope)
        if pending.action == SEND_ACTION:
            return workflow.execute_send(str(pending.approval_ref), scope)
        return {"status": "POLICY_DENIED", "retry_allowed": False}


__all__ = [
    "CREATE_ACTION", "SEND_ACTION", "MailchimpCampaignWorkflow",
    "UnifiedMailchimpApprovalCoordinator", "UnifiedMailchimpApprovalExecutor",
    "build_mailchimp_campaign_create_scope", "build_mailchimp_campaign_send_scope",
    "campaign_fingerprint",
]
