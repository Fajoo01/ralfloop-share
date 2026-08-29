from __future__ import annotations

import copy

from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant import runtime
from ralfloop_agent.unified_assistant.mailchimp_campaign import (
    MailchimpCampaignWorkflow, campaign_fingerprint,
)


class FakeProvider:
    def __init__(self):
        self.campaigns = {}
        self.create_calls = 0
        self.send_calls = 0

    def validate_create(self, _scope):
        return None

    def create_campaign(self, scope):
        self.create_calls += 1
        campaign_id = "campaign_runtime"
        self.campaigns[campaign_id] = {
            "campaign_id": campaign_id, "list_id": scope["list_id"],
            "subject": scope["subject"], "from_name": scope["from_name"],
            "reply_to": scope["reply_to"], "content_sha256": scope["body_sha256"],
            "html_sha256": scope["html_sha256"],
            "sent": False,
        }
        return {"campaign_id": campaign_id}

    def read_campaign(self, campaign_id):
        return copy.deepcopy(self.campaigns[campaign_id])

    def send_campaign(self, campaign_id):
        self.send_calls += 1
        self.campaigns[campaign_id]["sent"] = True


def draft():
    return {
        "source_draft_sha256": "4" * 64, "list_id": "audience_1",
        "subject": "Approved subject", "from_name": "TIREMM INNANZ APS",
        "reply_to": "info@example.invalid", "preheader": "Preview",
        "body_text": "Approved body", "html_body": "<html><body>Approved body</body></html>",
        "cta_label": "Details",
        "cta_target": "https://example.invalid/details", "internal_title": "Internal title",
        "provider_identity": "mailchimp:test",
    }


def configure(monkeypatch, tmp_path, provider):
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_MAILCHIMP_CAMPAIGN_LIVE", "1")
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "11")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "22")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approval.sqlite"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        runtime, "MailchimpApprovedMCPWorkflow",
        lambda: MailchimpCampaignWorkflow(DomainApprovalStore(), provider),
    )


def context(**extra):
    return {
        "source": "telegram_natural", "telegram_user_id": 11,
        "telegram_chat_id": 22, "telegram_message_id": extra.pop("message_id", 1),
        "telegram_chat_type": "private", **extra,
    }


def test_runtime_create_then_separate_send_approval(monkeypatch, tmp_path):
    provider = FakeProvider()
    configure(monkeypatch, tmp_path, provider)
    create_pending = runtime.run_unified_telegram(
        "crea una campagna Mailchimp", context(mailchimp_campaign_draft=draft()),
    )
    create_id = create_pending["metadata"]["approval_request_id"]
    created = runtime.run_unified_telegram("approvo", context(message_id=2))
    assert created["metadata"]["status"] == "executed"
    assert provider.create_calls == 1
    assert provider.send_calls == 0

    campaign = provider.read_campaign("campaign_runtime")
    verified = {
        **campaign, "provider_campaign_sha256": campaign_fingerprint(campaign),
        "provider_identity": "mailchimp:test",
    }
    send_pending = runtime.run_unified_telegram(
        "invia la campagna Mailchimp", context(message_id=3, mailchimp_campaign_verified=verified),
    )
    send_id = send_pending["metadata"]["approval_request_id"]
    assert create_id != send_id
    sent = runtime.run_unified_telegram("approvo", context(message_id=4))
    assert sent["metadata"]["status"] == "executed"
    assert provider.create_calls == 1
    assert provider.send_calls == 1


def test_runtime_without_approval_never_calls_provider(monkeypatch, tmp_path):
    provider = FakeProvider()
    configure(monkeypatch, tmp_path, provider)
    pending = runtime.run_unified_telegram(
        "crea una campagna Mailchimp", context(mailchimp_campaign_draft=draft()),
    )
    assert pending["metadata"]["status"] == "draft_pending_approval"
    assert provider.create_calls == 0
    assert provider.send_calls == 0
