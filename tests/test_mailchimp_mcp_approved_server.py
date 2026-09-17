from __future__ import annotations

import importlib.util
from pathlib import Path

from ralfloop_agent.domains.domain_approval import DomainApprovalDecision, DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.mailchimp_campaign import (
    CREATE_ACTION, SEND_ACTION, build_mailchimp_campaign_create_scope,
    build_mailchimp_campaign_send_scope, campaign_fingerprint,
)


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mailchimp_server_approved", ROOT / "scripts/ralf_mailchimp_mcp_server.py")
assert SPEC and SPEC.loader
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class FakeClient:
    def __init__(self):
        self.campaign = None
        self.create_calls = 0
        self.send_calls = 0

    def _request(self, path, **_kwargs):
        assert path == "lists/audience_1"
        return {"id": "audience_1"}

    def create_campaign(self, scope):
        self.create_calls += 1
        self.campaign = {
            "campaign_id": "campaign_1", "list_id": scope["list_id"],
            "subject": scope["subject"], "from_name": scope["from_name"],
            "reply_to": scope["reply_to"], "content_sha256": scope["body_sha256"],
            "html_sha256": scope["html_sha256"], "content_verified": True,
            "sent": False, "provider_status": "save",
        }
        return dict(self.campaign)

    def get_campaign(self, campaign_id):
        assert campaign_id == "campaign_1"
        return dict(self.campaign)

    def send_campaign(self, campaign_id):
        self.send_calls += 1
        assert campaign_id == "campaign_1"
        self.campaign["sent"] = True
        self.campaign["provider_status"] = "sent"
        return dict(self.campaign)


def store(tmp_path):
    policy = DomainApprovalPolicy(
        enabled=True, ttl_sec=300, allowed_user_ids={11}, allowed_chat_ids={22},
        db_path=str(tmp_path / "approval.sqlite"), audit_log=str(tmp_path / "audit.jsonl"),
    )
    return DomainApprovalStore(policy=policy)


def approved(db, action, scope, message):
    row = db.create_request(
        action=action, bando_id="mailchimp.marketing", version="1", scope=scope,
    )["request"]
    decision = db.decide(
        DomainApprovalDecision(row["request_id"], "approve", 11, 22, message,
                               idempotency_key=f"approved:{message}"),
        scope_digest_short=row["scope_digest_short"],
    )
    assert decision["status"] == "approved"
    return row["request_id"]


def arguments(scope, request_id):
    computed = {"action", "version", "artifact_sha256", "body_sha256"}
    if scope["action"] == CREATE_ACTION:
        computed.add("html_sha256")
    result = {
        key: value for key, value in scope.items()
        if key not in computed
    }
    result["approval_request_id"] = request_id
    result["execution_id"] = scope["execution_id"]
    return result


def create_scope():
    return build_mailchimp_campaign_create_scope({
        "draft_id": "draft_1", "draft_version": 1, "payload_digest": "1" * 64,
        "source_draft_sha256": "2" * 64, "list_id": "audience_1",
        "subject": "Subject", "from_name": "TIREMM INNANZ APS",
        "reply_to": "info@example.invalid", "preheader": "Preview",
        "body_text": "Body", "html_body": "<html><body>Body</body></html>",
        "cta_label": "Details",
        "cta_target": "https://example.invalid", "internal_title": "Internal",
        "provider_identity": "mailchimp:test",
    })


def test_server_requires_approval_then_creates_draft_only(tmp_path):
    db = store(tmp_path)
    client = FakeClient()
    server = SERVER.MailchimpMCPServer(client, approval_store=db)
    scope = create_scope()
    denied = server.call("mailchimp_create_approved_campaign", arguments(scope, "apr_ABCDEFGH"))
    assert denied["structuredContent"]["status"] == "APPROVAL_INVALID"
    assert client.create_calls == 0
    request_id = approved(db, CREATE_ACTION, scope, 1)
    result = server.call("mailchimp_create_approved_campaign", arguments(scope, request_id))
    assert result["state"] == "DRAFT"
    assert (client.create_calls, client.send_calls) == (1, 0)
    assert db.get_request(request_id)["status"] == "consumed"


def test_client_puts_exact_approved_html_and_plain_text():
    class RecordingClient(SERVER.MailchimpClient):
        def __init__(self):
            super().__init__("fake-us1", "us1")
            self.requests = []

        def _request(self, path, **kwargs):
            self.requests.append((path, kwargs))
            if path == "campaigns":
                return {"id": "campaign_1"}
            if path.endswith("/content") and kwargs.get("method") == "PUT":
                return {}
            if path.endswith("/content"):
                html = create_scope()["html_body"].replace(
                    "</body></html>",
                    '<center id="canspamBarWrapper">Mailchimp footer</center></body></html>',
                )
                return {
                    "html": html,
                    "plain_text": "Body\n==============================================\nUnsubscribe",
                }
            return {
                "id": "campaign_1", "status": "save",
                "recipients": {"list_id": "audience_1"},
                "settings": {
                    "subject_line": "Subject", "from_name": "TIREMM INNANZ APS",
                    "reply_to": "info@example.invalid",
                },
            }

    client = RecordingClient()
    scope = create_scope()
    observed = client.create_campaign(scope)
    put = next(call for call in client.requests if call[1].get("method") == "PUT")
    assert put == (
        "campaigns/campaign_1/content",
        {"method": "PUT", "body": {"plain_text": "Body", "html": scope["html_body"]}},
    )
    assert observed["content_verified"] is True


def test_server_send_needs_new_exact_approval(tmp_path):
    db = store(tmp_path)
    client = FakeClient()
    create = create_scope()
    client.create_campaign(create)
    campaign = client.get_campaign("campaign_1")
    send = build_mailchimp_campaign_send_scope({
        **campaign, "provider_campaign_sha256": campaign_fingerprint(campaign),
        "provider_identity": "mailchimp:test",
    })
    request_id = approved(db, SEND_ACTION, send, 2)
    result = SERVER.MailchimpMCPServer(client, approval_store=db).call(
        "mailchimp_send_approved_campaign", arguments(send, request_id),
    )
    assert result["state"] == "SENT"
    assert client.send_calls == 1
    assert db.get_request(request_id)["status"] == "consumed"


def test_client_empty_ok_accepts_empty_success_body(monkeypatch):
    class EmptyResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _limit):
            return b""

    monkeypatch.setattr(SERVER, "urlopen", lambda *_args, **_kwargs: EmptyResponse())
    client = SERVER.MailchimpClient("fake-us1", "us1")

    assert client._request(
        "campaigns/campaign_1/actions/send",
        method="POST",
        body={},
        empty_ok=True,
    ) == {}
