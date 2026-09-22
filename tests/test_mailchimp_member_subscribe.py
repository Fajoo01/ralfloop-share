import copy
import importlib.util
from pathlib import Path

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.mailchimp_campaign import (
    SUBSCRIBE_ACTION,
    MailchimpCampaignWorkflow,
    build_mailchimp_member_subscribe_scope,
)
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "mailchimp_server_member", ROOT / "scripts/ralf_mailchimp_mcp_server.py"
)
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class FakeProvider:
    def __init__(self):
        self.members = {}
        self.subscribe_calls = 0

    def read_member(self, list_id, email_address):
        row = self.members.get((list_id, email_address.casefold()))
        return copy.deepcopy(row) if row is not None else None

    def get_member(self, list_id, email_address):
        return self.read_member(list_id, email_address)

    def subscribe_member(self, scope):
        self.subscribe_calls += 1
        row = {
            "list_id": scope["list_id"],
            "email_address": scope["email_address"].casefold(),
            "status": "subscribed",
            "merge_fields": {
                "FNAME": scope.get("first_name", ""),
                "LNAME": scope.get("last_name", ""),
            },
        }
        self.members[(row["list_id"], row["email_address"])] = row
        return copy.deepcopy(row)


def store_for(tmp_path):
    policy = DomainApprovalPolicy(
        enabled=True,
        ttl_sec=300,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        db_path=str(tmp_path / "approval.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    return DomainApprovalStore(policy=policy)


def scope_for(email="Person@Example.invalid"):
    return build_mailchimp_member_subscribe_scope({
        "list_id": "80d24f2b10",
        "email_address": email,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "provider_identity": "mailchimp:test",
    })


def approve(store, scope, message_id=33):
    row = store.create_request(
        action=SUBSCRIBE_ACTION,
        bando_id="mailchimp.marketing",
        version="1",
        scope=scope,
    )["request"]
    result = store.decide(
        DomainApprovalDecision(
            row["request_id"],
            "approve",
            11,
            22,
            message_id,
            idempotency_key=f"member:{message_id}",
        ),
        scope_digest_short=row["scope_digest_short"],
    )
    assert result["status"] == "approved"
    return row["request_id"]


def server_arguments(scope, request_id):
    return {
        key: value
        for key, value in scope.items()
        if key not in {"action", "version", "artifact_sha256"}
    } | {
        "approval_request_id": request_id,
        "execution_id": scope["execution_id"],
    }


def test_scope_normalizes_email_and_binds_material_fields():
    baseline = scope_for()
    assert baseline["email_address"] == "person@example.invalid"
    assert baseline["execution_id"].startswith("mcsubscribe_")
    changed = scope_for("other@example.invalid")
    assert changed["artifact_sha256"] != baseline["artifact_sha256"]


def test_workflow_subscribes_new_member_once(tmp_path):
    store = store_for(tmp_path)
    scope = scope_for()
    request_id = approve(store, scope)
    provider = FakeProvider()
    workflow = MailchimpCampaignWorkflow(store, provider)

    result = workflow.execute_subscribe(request_id, scope)

    assert result["status"] == "executed"
    assert result["state"] == "SUBSCRIBED"
    assert provider.subscribe_calls == 1
    assert store.get_request(request_id)["status"] == "consumed"
    replay = workflow.execute_subscribe(request_id, scope)
    assert replay["status"] == "APPROVAL_INVALID"
    assert provider.subscribe_calls == 1


def test_existing_subscribed_member_consumes_without_write(tmp_path):
    store = store_for(tmp_path)
    scope = scope_for()
    request_id = approve(store, scope)
    provider = FakeProvider()
    provider.members[(scope["list_id"], scope["email_address"])] = {
        "list_id": scope["list_id"],
        "email_address": scope["email_address"],
        "status": "subscribed",
    }
    result = MailchimpCampaignWorkflow(store, provider).execute_subscribe(
        request_id, scope
    )
    assert result["status"] == "already_subscribed"
    assert provider.subscribe_calls == 0
    assert store.get_request(request_id)["status"] == "consumed"


def test_unsubscribed_member_requires_reconsent_and_no_write(tmp_path):
    store = store_for(tmp_path)
    scope = scope_for()
    request_id = approve(store, scope)
    provider = FakeProvider()
    provider.members[(scope["list_id"], scope["email_address"])] = {
        "list_id": scope["list_id"],
        "email_address": scope["email_address"],
        "status": "unsubscribed",
    }

    result = MailchimpCampaignWorkflow(store, provider).execute_subscribe(
        request_id, scope
    )
    assert result["status"] == "MEMBER_REQUIRES_RECONSENT"
    assert provider.subscribe_calls == 0
    assert store.get_request(request_id)["status"] == "stale"


def test_planner_routes_mailing_list_subscribe_to_confirm_write():
    plan = UnifiedPlanner(UnifiedRegistryFacade()).plan(
        "aggiungi person@example.invalid alla mailing list"
    )
    assignment = plan.assignments[0]
    assert plan.intent == "mailchimp.member.subscribe"
    assert assignment.policy.value == "CONFIRM_WRITE"
    assert assignment.arguments["action"] == SUBSCRIBE_ACTION
    assert assignment.arguments["email_address"] == "person@example.invalid"


def test_planner_keeps_ambiguous_mailchimp_mutation_denied():
    plan = UnifiedPlanner(UnifiedRegistryFacade()).plan(
        "Mailchimp: aggiungi un contatto"
    )
    assert plan.intent == "assistant.reject"


def test_server_subscribe_requires_exact_approval(tmp_path):
    store = store_for(tmp_path)
    scope = scope_for()
    client = FakeProvider()
    server = SERVER.MailchimpMCPServer(client, approval_store=store)

    denied = server.call(
        "mailchimp_subscribe_approved_member",
        server_arguments(scope, "apr_ABCDEFGH"),
    )
    assert denied["structuredContent"]["status"] == "APPROVAL_INVALID"
    assert client.subscribe_calls == 0
    request_id = approve(store, scope, message_id=44)
    result = server.call(
        "mailchimp_subscribe_approved_member",
        server_arguments(scope, request_id),
    )
    assert result["status"] == "executed"
    assert result["state"] == "SUBSCRIBED"
    assert result["writes"] == 1
    assert result["sends"] == 0
    assert client.subscribe_calls == 1
    assert store.get_request(request_id)["status"] == "consumed"


def test_server_does_not_reactivate_unsubscribed_member(tmp_path):
    store = store_for(tmp_path)
    scope = scope_for()
    request_id = approve(store, scope, message_id=45)
    client = FakeProvider()
    client.members[(scope["list_id"], scope["email_address"])] = {
        "list_id": scope["list_id"],
        "email_address": scope["email_address"],
        "status": "unsubscribed",
    }
    result = SERVER.MailchimpMCPServer(client, approval_store=store).call(
        "mailchimp_subscribe_approved_member",
        server_arguments(scope, request_id),
    )
    assert result["structuredContent"]["status"] == "MEMBER_REQUIRES_RECONSENT"
    assert client.subscribe_calls == 0


def test_runtime_stages_and_executes_subscribe_only_after_approval(monkeypatch, tmp_path):
    from ralfloop_agent.unified_assistant import runtime

    provider = FakeProvider()
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_MAILCHIMP_CAMPAIGN_LIVE", "1")
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "11")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "22")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approval.sqlite"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("RALF_MAILCHIMP_DEFAULT_LIST_ID", "80d24f2b10")
    monkeypatch.setattr(
        runtime, "MailchimpApprovedMCPWorkflow",
        lambda: MailchimpCampaignWorkflow(DomainApprovalStore(), provider),
    )
    context = {
        "source": "telegram_natural", "telegram_user_id": 11,
        "telegram_chat_id": 22, "telegram_message_id": 1,
        "telegram_chat_type": "private",
    }
    pending = runtime.run_unified_telegram(
        "aggiungi person@example.invalid alla mailing list", context,
    )
    assert pending["metadata"]["status"] == "draft_pending_approval"
    assert pending["metadata"]["approval_request_id"].startswith("apr_")
    assert provider.subscribe_calls == 0

    approved = runtime.run_unified_telegram(
        "approvo", {**context, "telegram_message_id": 2},
    )
    assert approved["metadata"]["status"] == "executed"
    assert provider.subscribe_calls == 1
    assert provider.members[("80d24f2b10", "person@example.invalid")]["status"] == "subscribed"


def test_route_probe_exposes_mailchimp_subscribe_as_policy_gated(monkeypatch):
    from ralfloop_agent.unified_assistant.runtime import unified_route_probe

    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    route = unified_route_probe(
        "aggiungi person@example.invalid alla mailing list",
        {
            "source": "telegram_natural", "telegram_user_id": 11,
            "telegram_chat_id": 22, "telegram_message_id": 1,
        },
    )
    assert route is not None
    assert route["task_mode"] == "external_action"
    assert route["skills_used"] == ["mailchimp.member.subscribe"]
    assert route["mcp_used"] == ["mailchimp.marketing"]
    assert route["requires_confirmation"] is True
    assert route["write_policy"] == "policy_gated"
