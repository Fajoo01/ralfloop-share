from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta

from ralfloop_agent.cli.session_store import SessionStore
from ralfloop_agent.unified_assistant.conversation import SessionConversationAdapter
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.email_inbox_trigger import GmailInboxTrigger
from ralfloop_agent.unified_assistant.event_router import EventRouter
from ralfloop_agent.unified_assistant.memory_service import MemoryService


NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
ACCOUNT = "fabio@tiremminnanz.com"


class Gateway:
    def __init__(self, rows, messages):
        self.rows = rows
        self.messages = messages
        self.calls = []

    def invoke(self, operation, **arguments):
        self.calls.append((operation, dict(arguments)))
        if operation == "search":
            return {"messages": list(self.rows)}
        if operation == "read":
            return {"message": dict(self.messages[arguments["messageId"]])}
        raise AssertionError(operation)


class Context(AbstractContextManager):
    def __init__(self, gateway):
        self.gateway = gateway

    def __enter__(self):
        return self.gateway

    def __exit__(self, *_):
        return None


def message(mid, sender="Irene <irene@example.org>", *, date=None, labels="IMPORTANT, INBOX"):
    stamp = date or NOW
    return {
        "messageId": mid,
        "threadId": "",
        "from": sender,
        "to": ACCOUNT,
        "subject": "Bando",
        "date": stamp.strftime("%a, %d %b %Y %H:%M:%S +0000"),
        "labels": labels,
        "body": "Possiamo sentirci per il progetto?",
    }


def build(tmp_path, gateway, runner):
    memory = MemoryService(tmp_path / "memory.sqlite")
    trigger = GmailInboxTrigger(
        gateway_factory=lambda: Context(gateway),
        memory=memory, router=EventRouter(memory), response_runner=runner,
        account=ACCOUNT, telegram_user_id=11, telegram_chat_id=22,
        session_root=tmp_path / "sessions", state_path=tmp_path / "trigger.json",
        now=lambda: NOW,
    )
    return memory, trigger


def test_first_poll_bootstraps_without_processing_existing_mail(tmp_path):
    gateway = Gateway([{"messageId": "m1"}], {"m1": message("m1")})
    calls = []
    memory, trigger = build(tmp_path, gateway, lambda *args: calls.append(args))
    try:
        result = trigger.poll()
        assert result.bootstrapped is True
        assert result.discovered == result.queued == result.drafted == 0
        assert calls == []
        assert memory.list_entities(domain="email_intake") == ()
    finally:
        memory.close()


def test_new_message_queues_event_and_creates_one_approval_bound_draft(tmp_path):
    gateway = Gateway([{"messageId": "m1"}], {"m1": message("m1")})
    runner_calls = []

    def runner(instruction, context):
        runner_calls.append((instruction, dict(context)))
        return {
            "metadata": {
                "status": "draft_pending_approval",
                "pending_id": "pending_1234567890abcdef",
                "approval_request_id": "apr_ABCDEFGH",
            }
        }

    memory, trigger = build(tmp_path, gateway, runner)
    try:
        trigger.poll()
        gateway.rows = [{"messageId": "m2"}, {"messageId": "m1"}]
        gateway.messages["m2"] = message("m2", date=NOW + timedelta(minutes=1))
        result = trigger.poll()
        assert result.discovered == result.queued == result.drafted == 1
        assert len(runner_calls) == 1
        assert "Destinatario verificato: irene@example.org" in runner_calls[0][0]
        assert "Messaggio Gmail sorgente: m2" in runner_calls[0][0]
        candidate = memory.get_entity("email-reply-m2")
        assert candidate is not None and candidate.status == "PENDING_APPROVAL"
        assert candidate.data["approval_request_id"] == "apr_ABCDEFGH"
        events = memory.timeline("email-reply-m2")
        assert len(events) == 1 and events[0].type == "EMAIL_RECEIVED"
    finally:
        memory.close()


def test_existing_pending_email_keeps_new_messages_queued(tmp_path):
    gateway = Gateway([{"messageId": "m1"}], {"m1": message("m1")})
    calls = []
    memory, trigger = build(tmp_path, gateway, lambda *args: calls.append(args))
    try:
        trigger.poll()
        store = SessionStore(tmp_path / "sessions")
        from ralfloop_agent.unified_assistant.runtime import _ensure_session
        _ensure_session(store, "telegram-22-11")
        adapter = SessionConversationAdapter(store)
        conversation = adapter.load("telegram-22-11")
        conversation.stage(
            domain="email", action="send_email", policy=PolicyClass.CONFIRM_WRITE,
            payload={"recipient": "x@example.org", "body": "draft"},
            displayed_text="draft",
        )
        adapter.save("telegram-22-11", conversation)
        gateway.rows = [{"messageId": "m2"}, {"messageId": "m1"}]
        gateway.messages["m2"] = message("m2", date=NOW + timedelta(minutes=1))
        result = trigger.poll()
        assert result.busy is True and result.queued == 1 and result.drafted == 0
        assert memory.get_entity("email-reply-m2").status == "QUEUED"
        assert calls == []
    finally:
        memory.close()


def test_self_promotional_and_noreply_are_skipped(tmp_path):
    gateway = Gateway([{"messageId": "m1"}], {"m1": message("m1")})
    memory, trigger = build(tmp_path, gateway, lambda *_: None)
    try:
        trigger.poll()
        gateway.rows = [
            {"messageId": "self"}, {"messageId": "promo"},
            {"messageId": "noreply"}, {"messageId": "m1"},
        ]
        gateway.messages.update({
            "self": message("self", sender=f"Fabio <{ACCOUNT}>", date=NOW + timedelta(minutes=1)),
            "promo": message("promo", sender="Shop <shop@example.org>", date=NOW + timedelta(minutes=1), labels="INBOX, CATEGORY_PROMOTIONS"),
            "noreply": message("noreply", sender="No Reply <no-reply@example.org>", date=NOW + timedelta(minutes=1)),
        })
        result = trigger.poll()
        assert result.discovered == 3 and result.skipped == 3 and result.queued == 0
    finally:
        memory.close()
