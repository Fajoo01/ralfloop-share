from __future__ import annotations

import json
import os

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.semantic_judge import assess_email_risk
from ralfloop_agent.unified_assistant.browser_read_only import FixedScriptResult
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.conversation import ConversationManager
from ralfloop_agent.unified_assistant.whatsapp_send import (
    UnifiedWhatsAppApprovalCoordinator,
    build_whatsapp_approval_scope,
)
from ralfloop_agent.unified_assistant.whatsapp_navigation import (
    _READ_CURRENT_CHAT_SCRIPT,
    ChatInventoryItem,
    CurrentChat,
    VisibleMessage,
    WhatsAppCdpNavigator,
)
from ralfloop_agent.unified_assistant.whatsapp_writer import WhatsAppBrowserWriter
from scripts.ralf_whatsapp_mcp_server import WhatsAppMCPServer
from src.google_workspace import _email_write_confirmation


CHAT = "wa_chat_" + "a" * 16
MESSAGE = "wa_msg_" + "b" * 16


def test_outbound_detection_uses_message_direction_container():
    assert "message-out" in _READ_CURRENT_CHAT_SCRIPT


def _approved_store(tmp_path):
    policy = DomainApprovalPolicy(
        enabled=True,
        ttl_sec=300,
        max_pending=10,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        require_private_chat=True,
        db_path=str(tmp_path / "approval.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    store = DomainApprovalStore(policy=policy)
    created = store.create_request(
        action="whatsapp_send", bando_id="whatsapp.web.work", version="1",
        scope={"action": "whatsapp_send"}, requested_by="test",
    )["request"]
    result = store.decide(
        DomainApprovalDecision(
            created["request_id"], "approve", 11, 22, 33,
            idempotency_key="approve-once",
        ),
        scope_digest_short=created["scope_digest_short"],
    )
    assert result["status"] == "approved"
    return store, created["request_id"]


def test_approval_database_is_private_and_stale_cannot_clobber_execution(tmp_path):
    store, request_id = _approved_store(tmp_path)
    assert os.stat(store.db_path).st_mode & 0o777 == 0o600

    assert store.claim_execution(request_id, action="whatsapp_send")["claimed"] is True
    stale = store.mark_stale(request_id, ["late_scope_mismatch"])

    assert stale["status"] == "already_executing"
    assert store.get_request(request_id)["status"] == "executing"


def test_stale_cannot_reopen_or_relabel_consumed_request(tmp_path):
    store, request_id = _approved_store(tmp_path)
    store.claim_execution(request_id, action="whatsapp_send")
    store.finish_claimed_execution(
        request_id, action="whatsapp_send", success=True,
        result={"status": "executed"},
    )

    assert store.mark_stale(request_id, ["late"])["status"] == "already_consumed"
    assert store.get_request(request_id)["status"] == "consumed"


class DuplicateChatClient:
    def evaluate_fixed(self, **_kwargs):
        return FixedScriptResult(
            value=[
                {"title": "Marco", "secondary": "one"},
                {"title": "Marco", "secondary": "two"},
            ],
            operation="whatsapp.list_chats",
        )


def test_duplicate_chat_titles_are_preserved_as_ambiguity():
    rows = WhatsAppCdpNavigator(DuplicateChatClient(), wait_seconds=0).list_chats()

    assert len(rows) == 2
    assert rows[0].chat_id == rows[1].chat_id


class WriterClient:
    def __init__(self):
        self.inserted: list[str] = []

    def insert_read_navigation_text(self, text):
        self.inserted.append(text)


class WriterProvider:
    def __init__(self, observed_text):
        self.client = WriterClient()
        self.navigator = type("Navigator", (), {"client": self.client})()
        self.observed_text = observed_text
        self.read_count = 0

    def list_chats(self):
        return (ChatInventoryItem(chat_id=CHAT, title="Marco"),)

    def open_chat(self, _chat_id):
        return {"status": "OPENED"}

    def read_messages(self, _chat_id, *, limit):
        self.read_count += 1
        if self.read_count == 1:
            return CurrentChat(status="FOUND", chat_id=CHAT, title="Marco")
        return CurrentChat(
            status="FOUND", chat_id=CHAT, title="Marco",
            messages=(VisibleMessage(
                message_id=MESSAGE, raw_ref="raw_true_b", text=self.observed_text,
                kind="text", provenance_ref="whatsapp_web:test", outbound=True,
            ),),
        )


def _writer(provider):
    writer = WhatsAppBrowserWriter(provider, wait_seconds=0)
    writer._function = lambda function, _arguments: {
        "status": "ARMED" if "DRAFT_PRESENT" in function else "SUBMITTED"
    }
    return writer


def test_writer_sends_exact_approved_spacing_and_verifies_exact_text():
    provider = WriterProvider("Va  bene")
    result = _writer(provider).send_message(
        chat_id=CHAT, chat_title="Marco", body="Va  bene", execution_id="waexec_test",
    )

    assert "".join(provider.client.inserted) == "Va  bene"
    assert result["verified"] is True


def test_writer_rejects_whitespace_or_case_false_positive_and_changed_chat_title():
    provider = WriterProvider("va bene")
    result = _writer(provider).send_message(
        chat_id=CHAT, chat_title="Marco", body="Va bene", execution_id="waexec_test",
    )
    assert result["status"] == "SEND_UNVERIFIED"

    provider = WriterProvider("Va bene")
    result = _writer(provider).send_message(
        chat_id=CHAT, chat_title="Other", body="Va bene", execution_id="waexec_test",
    )
    assert result["status"] == "CHAT_IDENTITY_CHANGED"
    assert provider.client.inserted == []


def test_gmail_confirmation_ignores_unrelated_nested_id():
    assert _email_write_confirmation({
        "structuredContent": {"account": {"id": "0123456789abcdef"}}
    })["message_id"] == ""
    assert _email_write_confirmation({
        "structuredContent": {"refs": {"id": "fedcba9876543210"}}
    })["message_id"] == "fedcba9876543210"


def test_uncertain_external_participation_role_routes_high():
    packet = {
        "source_email": {
            "sender": "External partner", "subject": "Project collaboration", "body": "",
        },
        "organization_context": {
            "name": "Tiremm Innanz APS", "relevant_facts": [],
            "signature": {"required": False, "name": "", "organization": ""},
        },
        "user_intent": [
            "Ci interessa partecipare al progetto, ma dobbiamo ancora capire quale ruolo possiamo avere."
        ],
        "reply_constraints": {"no_new_commitments": True},
    }

    result = assess_email_risk(packet, "Ci farebbe piacere partecipare.")

    assert result.level == "high"
    assert "uncertain_participation_role" in result.reasons


def test_unified_write_approval_fails_closed_without_telegram_allowlists(tmp_path):
    policy = DomainApprovalPolicy(
        enabled=True, db_path=str(tmp_path / "approval.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    store = DomainApprovalStore(policy=policy)
    pending = ConversationManager().stage(
        domain="whatsapp", action="whatsapp_send", policy=PolicyClass.CONFIRM_WRITE,
        payload={"chat_id": CHAT, "chat_title": "Marco", "body": "Va bene"},
        displayed_text="WhatsApp a Marco: Va bene. Invio?",
    )

    result = UnifiedWhatsAppApprovalCoordinator(store, policy=policy).request(
        pending, requested_by="test",
    )

    assert result["status"] == "approval_allowlist_unconfigured"
    assert store.count_pending() == 0


def test_mcp_write_rechecks_expiry_after_gateway_claim(tmp_path):
    store, request_id = _approved_store(tmp_path)
    # Replace the generic fixture scope with the exact writer contract.
    manager = ConversationManager()
    pending = manager.stage(
        domain="whatsapp", action="whatsapp_send", policy=PolicyClass.CONFIRM_WRITE,
        payload={
            "chat_id": CHAT, "chat_title": "Marco", "recipient": "Marco",
            "body": "Va bene", "risk": "low", "validation_state": "passed",
        },
        displayed_text="WhatsApp a Marco: Va bene. Invio?",
    )
    scope = build_whatsapp_approval_scope(pending)
    with store.connect() as conn:
        conn.execute(
            "update approval_requests set scope_json=?, scope_digest=?, status='approved' "
            "where request_id=?",
            (
                json.dumps(scope, ensure_ascii=False, sort_keys=True),
                scope_digest(scope),
                request_id,
            ),
        )
    assert store.claim_execution(request_id, action="whatsapp_send")["claimed"] is True
    with store.connect() as conn:
        conn.execute(
            "update approval_requests set expires_at=0 where request_id=?", (request_id,),
        )

    class NeverWriter:
        calls = []

        def send_message(self, **kwargs):
            self.calls.append(kwargs)
            raise AssertionError("expired write reached browser")

    writer = NeverWriter()
    server = WhatsAppMCPServer(object(), writer=writer, approval_store=store)
    result = server.call("whatsapp_send_message", {
        "execution_id": scope["execution_id"], "approval_request_id": request_id,
        "chat_id": CHAT, "chat_title": "Marco", "body": "Va bene",
        "draft_hash": scope["artifact_sha256"],
    })["structuredContent"]

    assert result["status"] == "APPROVAL_INVALID"
    assert writer.calls == []
