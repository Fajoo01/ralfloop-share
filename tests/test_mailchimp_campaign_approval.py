from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor

import pytest

from ralfloop_agent.domains.domain_approval import DomainApprovalDecision, DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.mailchimp_campaign import (
    CREATE_ACTION,
    SEND_ACTION,
    MailchimpCampaignWorkflow,
    UnifiedMailchimpApprovalCoordinator,
    build_mailchimp_campaign_create_scope,
    build_mailchimp_campaign_send_scope,
    campaign_fingerprint,
)
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.conversation import ConversationManager


class FakeProvider:
    def __init__(self) -> None:
        self.campaigns: dict[str, dict] = {}
        self.create_calls = 0
        self.send_calls = 0
        self.fail_validate = False
        self.fail_create = False
        self.fail_send = False
        self.create_mismatch = False
        self.send_mismatch = False

    def validate_create(self, _scope):
        if self.fail_validate:
            raise ConnectionError("preflight")

    def create_campaign(self, scope):
        self.create_calls += 1
        if self.fail_create:
            raise ConnectionError("uncertain")
        campaign_id = "campaign_123"
        self.campaigns[campaign_id] = {
            "campaign_id": campaign_id,
            "list_id": scope["list_id"],
            "subject": "changed" if self.create_mismatch else scope["subject"],
            "from_name": scope["from_name"],
            "reply_to": scope["reply_to"],
            "content_sha256": scope["body_sha256"],
            "sent": False,
        }
        return {"campaign_id": campaign_id}

    def read_campaign(self, campaign_id):
        return copy.deepcopy(self.campaigns[campaign_id])

    def send_campaign(self, campaign_id):
        self.send_calls += 1
        if self.fail_send:
            raise ConnectionError("uncertain")
        if not self.send_mismatch:
            self.campaigns[campaign_id]["sent"] = True


def create_payload():
    return {
        "draft_id": "meet-code-2026-v1",
        "draft_version": 1,
        "payload_digest": "a" * 64,
        "source_draft_sha256": "4505a7a4b8e3e4a0d392845e1ce1138773e580112ffabfcd4b34300fde90f280",
        "list_id": "80d24f2b10",
        "subject": "RoboYoga AILab: laboratorio gratuito",
        "from_name": "TIREMM INNANZ APS",
        "reply_to": "info@example.invalid",
        "preheader": "Laboratorio gratuito",
        "body_text": "Testo approvato.",
        "cta_label": "Scopri il progetto",
        "cta_target": "https://example.invalid/project",
        "internal_title": "Meet and Code 2026",
        "provider_identity": "mailchimp:test-account",
    }


def store_for(tmp_path):
    policy = DomainApprovalPolicy(
        enabled=True, ttl_sec=300, max_pending=20,
        allowed_user_ids={11}, allowed_chat_ids={22}, require_private_chat=True,
        db_path=str(tmp_path / "approvals.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    return DomainApprovalStore(policy=policy)


def approve(store, action, scope):
    created = store.create_request(
        action=action, bando_id="mailchimp.marketing", version="1",
        scope=dict(scope), requested_by="test",
    )["request"]
    result = store.decide(
        DomainApprovalDecision(
            created["request_id"], "approve", 11, 22, 33,
            chat_type="private", idempotency_key=created["request_id"],
        ),
        scope_digest_short=created["scope_digest_short"],
    )
    assert result["status"] == "approved"
    return created["request_id"]


def prepared_send(store, provider):
    create_scope = build_mailchimp_campaign_create_scope(create_payload())
    create_id = approve(store, CREATE_ACTION, create_scope)
    assert MailchimpCampaignWorkflow(store, provider).execute_create(create_id, create_scope)["state"] == "DRAFT"
    current = provider.read_campaign("campaign_123")
    send_scope = build_mailchimp_campaign_send_scope({
        **current,
        "provider_campaign_sha256": campaign_fingerprint(current),
        "provider_identity": "mailchimp:test-account",
    })
    return send_scope


def test_create_scope_binds_every_material_field():
    baseline = build_mailchimp_campaign_create_scope(create_payload())
    for field in ("list_id", "subject", "body_text", "cta_label", "cta_target"):
        changed = create_payload()
        changed[field] += "-changed"
        assert build_mailchimp_campaign_create_scope(changed)["artifact_sha256"] != baseline["artifact_sha256"]
    assert baseline["body_sha256"]
    assert baseline["execution_id"].startswith("mccreate_")


def test_no_approval_rejected_without_mutation(tmp_path):
    provider = FakeProvider()
    result = MailchimpCampaignWorkflow(store_for(tmp_path), provider).execute_create(
        "missing", build_mailchimp_campaign_create_scope(create_payload())
    )
    assert result["status"] == "APPROVAL_INVALID"
    assert provider.create_calls == 0


@pytest.mark.parametrize("field", ["list_id", "subject", "body_text", "cta_label", "cta_target"])
def test_changed_create_scope_becomes_stale(tmp_path, field):
    store = store_for(tmp_path)
    original = build_mailchimp_campaign_create_scope(create_payload())
    request_id = approve(store, CREATE_ACTION, original)
    changed = create_payload()
    changed[field] += "-changed"
    provider = FakeProvider()
    result = MailchimpCampaignWorkflow(store, provider).execute_create(
        request_id, build_mailchimp_campaign_create_scope(changed)
    )
    assert result["status"] == "DRAFT_CHANGED"
    assert store.get_request(request_id)["status"] == "stale"
    assert provider.create_calls == 0


def test_wrong_telegram_identity_cannot_approve(tmp_path):
    store = store_for(tmp_path)
    scope = build_mailchimp_campaign_create_scope(create_payload())
    request = store.create_request(action=CREATE_ACTION, bando_id="mailchimp.marketing", version="1", scope=scope)["request"]
    for user_id, chat_id in ((99, 22), (11, 99)):
        result = store.decide(
            DomainApprovalDecision(request["request_id"], "approve", user_id, chat_id, user_id + chat_id,
                                   idempotency_key=f"{user_id}:{chat_id}"),
            scope_digest_short=request["scope_digest_short"],
        )
        assert result["status"] in {"unauthorized", "user_unauthorized", "chat_unauthorized"}
    assert store.get_request(request["request_id"])["status"] == "pending"


def test_coordinator_uses_shared_store_and_exact_pending_scope(tmp_path):
    store = store_for(tmp_path)
    manager = ConversationManager()
    pending = manager.stage(
        domain="mailchimp", action=CREATE_ACTION, policy=PolicyClass.CONFIRM_WRITE,
        payload=create_payload(), displayed_text="Campaign draft; approval required.",
    )
    coordinator = UnifiedMailchimpApprovalCoordinator(store, policy=store.policy)
    request = coordinator.request(pending, requested_by="test")
    pending = manager.attach_approval_request(
        domain="mailchimp", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest,
        approval_ref=request["request_id"], created_at=request["created_at"],
        expires_at=request["expires_at"],
    )
    denied = coordinator.approve(
        pending, telegram_user_id=99, telegram_chat_id=22,
        telegram_message_id=44, chat_type="private",
    )
    approved = coordinator.approve(
        pending, telegram_user_id=11, telegram_chat_id=22,
        telegram_message_id=45, chat_type="private",
    )
    assert denied["status"] == "unauthorized"
    assert approved["status"] == "approved"
    assert store.get_request(request["request_id"])["scope"]["list_id"] == "80d24f2b10"


@pytest.mark.parametrize("terminal", ["rejected", "stale", "expired"])
def test_terminal_approval_never_mutates(tmp_path, terminal):
    store = store_for(tmp_path)
    scope = build_mailchimp_campaign_create_scope(create_payload())
    request_id = approve(store, CREATE_ACTION, scope)
    with store.connect() as conn:
        if terminal == "expired":
            conn.execute("update approval_requests set expires_at=0 where request_id=?", (request_id,))
        else:
            conn.execute("update approval_requests set status=? where request_id=?", (terminal, request_id))
    provider = FakeProvider()
    assert MailchimpCampaignWorkflow(store, provider).execute_create(request_id, scope)["status"] == "APPROVAL_INVALID"
    assert provider.create_calls == 0


def test_create_is_one_shot_and_cannot_authorize_send(tmp_path):
    store = store_for(tmp_path)
    provider = FakeProvider()
    scope = build_mailchimp_campaign_create_scope(create_payload())
    request_id = approve(store, CREATE_ACTION, scope)
    workflow = MailchimpCampaignWorkflow(store, provider)
    assert workflow.execute_create(request_id, scope)["state"] == "DRAFT"
    assert workflow.execute_create(request_id, scope)["status"] == "APPROVAL_INVALID"
    send_scope = build_mailchimp_campaign_send_scope({
        **provider.read_campaign("campaign_123"),
        "provider_campaign_sha256": campaign_fingerprint(provider.read_campaign("campaign_123")),
    })
    assert workflow.execute_send(request_id, send_scope)["status"] == "APPROVAL_INVALID"
    assert provider.create_calls == 1
    assert provider.send_calls == 0


def test_concurrent_claim_allows_one_provider_mutation(tmp_path):
    store = store_for(tmp_path)
    provider = FakeProvider()
    scope = build_mailchimp_campaign_create_scope(create_payload())
    request_id = approve(store, CREATE_ACTION, scope)

    def execute():
        return MailchimpCampaignWorkflow(store, provider).execute_create(request_id, scope)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: execute(), range(2)))
    assert provider.create_calls == 1
    assert sum(result.get("created") is True for result in results) == 1


def test_send_is_separate_one_shot_and_exact_campaign_bound(tmp_path):
    store = store_for(tmp_path)
    provider = FakeProvider()
    scope = prepared_send(store, provider)
    request_id = approve(store, SEND_ACTION, scope)
    workflow = MailchimpCampaignWorkflow(store, provider)
    wrong = {**scope, "campaign_id": "wrong"}
    assert workflow.execute_send(request_id, wrong)["status"] == "DRAFT_CHANGED"
    assert provider.send_calls == 0


def test_provider_change_before_send_marks_stale(tmp_path):
    store = store_for(tmp_path)
    provider = FakeProvider()
    scope = prepared_send(store, provider)
    request_id = approve(store, SEND_ACTION, scope)
    provider.campaigns["campaign_123"]["subject"] = "provider changed"
    result = MailchimpCampaignWorkflow(store, provider).execute_send(request_id, scope)
    assert result["status"] == "DRAFT_CHANGED"
    assert provider.send_calls == 0


@pytest.mark.parametrize("phase", ["preflight", "create", "create_postcondition"])
def test_create_failure_retry_policy_depends_on_claim(tmp_path, phase):
    store = store_for(tmp_path)
    provider = FakeProvider()
    setattr(provider, {"preflight": "fail_validate", "create": "fail_create", "create_postcondition": "create_mismatch"}[phase], True)
    scope = build_mailchimp_campaign_create_scope(create_payload())
    request_id = approve(store, CREATE_ACTION, scope)
    workflow = MailchimpCampaignWorkflow(store, provider)
    result = workflow.execute_create(request_id, scope)
    if phase == "preflight":
        assert result["retry_allowed"] is True
        assert store.get_request(request_id)["status"] == "approved"
        assert provider.create_calls == 0
    else:
        assert result["retry_allowed"] is False
        assert store.get_request(request_id)["status"] == "execution_failed"
        calls = provider.create_calls
        workflow.execute_create(request_id, scope)
        assert provider.create_calls == calls


@pytest.mark.parametrize("failure", ["exception", "postcondition"])
def test_send_uncertain_outcome_is_not_retryable(tmp_path, failure):
    store = store_for(tmp_path)
    provider = FakeProvider()
    scope = prepared_send(store, provider)
    request_id = approve(store, SEND_ACTION, scope)
    setattr(provider, "fail_send" if failure == "exception" else "send_mismatch", True)
    workflow = MailchimpCampaignWorkflow(store, provider)
    result = workflow.execute_send(request_id, scope)
    assert result["retry_allowed"] is False
    assert store.get_request(request_id)["status"] == "execution_failed"
    workflow.execute_send(request_id, scope)
    assert provider.send_calls == 1


def test_send_approval_cannot_authorize_create(tmp_path):
    store = store_for(tmp_path)
    provider = FakeProvider()
    scope = prepared_send(store, provider)
    request_id = approve(store, SEND_ACTION, scope)
    result = MailchimpCampaignWorkflow(store, provider).execute_create(
        request_id, build_mailchimp_campaign_create_scope(create_payload())
    )
    assert result["status"] == "POLICY_DENIED"
    assert provider.create_calls == 1


def test_send_success_is_consumed_once(tmp_path):
    store = store_for(tmp_path)
    provider = FakeProvider()
    scope = prepared_send(store, provider)
    request_id = approve(store, SEND_ACTION, scope)
    workflow = MailchimpCampaignWorkflow(store, provider)
    result = workflow.execute_send(request_id, scope)
    assert result["state"] == "SENT"
    assert provider.send_calls == 1
    assert store.get_request(request_id)["status"] == "consumed"


def test_mailchimp_specific_reconciliation_uses_campaign_evidence(tmp_path):
    store = store_for(tmp_path)
    provider = FakeProvider()
    scope = prepared_send(store, provider)
    request_id = approve(store, SEND_ACTION, scope)
    provider.campaigns["campaign_123"]["sent"] = True
    with store.connect() as conn:
        conn.execute(
            "update approval_requests set status='execution_failed' where request_id=?",
            (request_id,),
        )
    workflow = MailchimpCampaignWorkflow(store, provider)
    result = workflow.reconcile(request_id, action=SEND_ACTION, campaign_id="campaign_123")
    assert result["reconciled"] is True
    assert workflow.reconcile(
        request_id, action=SEND_ACTION, campaign_id="campaign_123"
    )["status"] == "already_reconciled"


def test_planner_only_routes_explicit_guarded_campaign_intents():
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    assert planner.plan("crea la campagna Mailchimp dalla bozza approvata").assignments[0].policy.value == "CONFIRM_WRITE"
    assert planner.plan("invia la campagna Mailchimp già preparata").assignments[0].policy.value == "CONFIRM_WRITE"
    assert planner.plan("invia campagna Mailchimp").assignments[0].policy.value == "CONFIRM_WRITE"
    assert planner.plan("elenca audience Mailchimp").assignments[0].policy.value == "READ"
    assert planner.plan("preparami una newsletter").assignments[0].policy.value != "CONFIRM_WRITE"


def test_registry_separates_read_and_approval_bound_capabilities():
    tools = {tool.id: tool for tool in UnifiedRegistryFacade().list_tools()}
    read = tools["mailchimp.marketing.read_only"]
    protected = tools["mailchimp.marketing.approval_bound"]
    assert len(read.capabilities) == 7
    assert all(cap.endswith(".read") or cap == "mailchimp.ping" for cap in read.capabilities)
    assert protected.classification.value == "CONFIRM_WRITE"
    assert protected.capabilities == (
        "mailchimp.campaign.create.approved", "mailchimp.campaign.send.approved"
    )
