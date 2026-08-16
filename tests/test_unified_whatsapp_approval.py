from __future__ import annotations

from contextlib import AbstractContextManager

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.conversation import ConversationManager
from ralfloop_agent.unified_assistant.whatsapp_send import (
    UnifiedWhatsAppApprovalCoordinator,
    UnifiedWhatsAppApprovalExecutor,
)


CHAT = "wa_chat_" + "a" * 16
MESSAGE = "wa_msg_" + "b" * 16


class FakeGateway:
    def __init__(self, *, status="verified"):
        self.status = status
        self.calls = []

    def execute_approved(self, store, request_id, scope):
        self.calls.append((request_id, dict(scope)))
        claim = store.claim_execution(request_id, action=scope["action"])
        if not claim.get("claimed"):
            return {"status": claim["status"], "sent": False}
        if self.status == "raise":
            raise RuntimeError("browser_died_after_claim")
        if self.status == "unverified":
            result = {"status": "SEND_UNVERIFIED", "sent": False, "retry_allowed": False}
            store.finish_claimed_execution(request_id, action=scope["action"], success=False, result=result)
            return result
        result = {
            "status": "executed", "sent": True, "verified": True,
            "message_id": "wa_msg_" + "c" * 16, "approved_hash": scope["artifact_sha256"],
        }
        store.finish_claimed_execution(request_id, action=scope["action"], success=True, result=result)
        return result


class Context(AbstractContextManager):
    def __init__(self, gateway):
        self.gateway = gateway
    def __enter__(self):
        return self.gateway
    def __exit__(self, *_):
        return None


def _approved(tmp_path, *, reply=False):
    policy = DomainApprovalPolicy(
        enabled=True, ttl_sec=300, max_pending=10,
        allowed_user_ids={11}, allowed_chat_ids={22}, require_private_chat=True,
        db_path=str(tmp_path / "approvals.sqlite"), audit_log=str(tmp_path / "audit.jsonl"),
    )
    store = DomainApprovalStore(policy=policy)
    manager = ConversationManager()
    pending = manager.stage(
        domain="whatsapp", action="whatsapp_reply" if reply else "whatsapp_send",
        policy=PolicyClass.CONFIRM_WRITE,
        payload={
            "chat_id": CHAT, "reply_to_message_id": MESSAGE if reply else "",
            "recipient": "Marco", "body": "Va bene, grazie.", "risk": "low",
            "validation_state": "passed",
        },
        displayed_text="WhatsApp a Marco: Va bene, grazie. Invio?",
    )
    coordinator = UnifiedWhatsAppApprovalCoordinator(store, policy=policy)
    requested = coordinator.request(pending, requested_by="test")
    pending = manager.attach_approval_request(
        domain="whatsapp", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest, approval_ref=requested["request_id"],
        created_at=requested["created_at"], expires_at=requested["expires_at"],
    )
    approved = coordinator.approve(
        pending, telegram_user_id=11, telegram_chat_id=22,
        telegram_message_id=33, chat_type="private",
    )
    assert approved["status"] == "approved"
    pending = manager.bind_approval(
        domain="whatsapp", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest, approval_ref=str(pending.approval_ref),
    )
    return store, manager, pending


def _executor(store, gateway):
    return UnifiedWhatsAppApprovalExecutor(lambda: Context(gateway), store=store)


def test_exact_hash_version_chat_bound_send_is_one_shot(tmp_path):
    store, _manager, pending = _approved(tmp_path)
    gateway = FakeGateway()
    executor = _executor(store, gateway)

    first = executor.execute(pending)
    second = executor.execute(pending)

    assert first["status"] == "executed"
    assert first["verified"] is True
    assert second["status"] == "already_executed"
    assert len(gateway.calls) == 1
    assert store.get_request(str(pending.approval_ref))["status"] == "consumed"


def test_reply_preserves_exact_chat_and_message_binding(tmp_path):
    store, _manager, pending = _approved(tmp_path, reply=True)
    gateway = FakeGateway()

    result = _executor(store, gateway).execute(pending)

    scope = gateway.calls[0][1]
    assert result["status"] == "executed"
    assert scope["chat_id"] == CHAT
    assert scope["reply_to_message_id"] == MESSAGE


def test_changed_draft_cannot_use_old_approval(tmp_path):
    store, _manager, pending = _approved(tmp_path)
    changed = pending.model_copy(update={"payload": {**pending.payload, "body": "Changed"}})
    gateway = FakeGateway()

    result = _executor(store, gateway).execute(changed)

    assert result["status"] == "APPROVAL_REQUIRED"
    assert gateway.calls == []


def test_unverified_or_crashed_send_blocks_blind_retry(tmp_path):
    for mode in ("unverified", "raise"):
        run = tmp_path / mode
        run.mkdir()
        store, _manager, pending = _approved(run)
        gateway = FakeGateway(status=mode)
        executor = _executor(store, gateway)

        first = executor.execute(pending)
        second = executor.execute(pending)

        assert first["status"] in {"SEND_UNVERIFIED", "EXECUTION_UNCERTAIN"}
        assert first["retry_allowed"] is False
        assert second["status"] == "EXECUTION_UNCERTAIN"
        assert len(gateway.calls) == 1


def test_two_pending_or_expired_pending_never_resolves_generic_ok():
    manager = ConversationManager()
    manager.stage(
        domain="whatsapp", action="whatsapp_send", policy=PolicyClass.CONFIRM_WRITE,
        payload={"chat_id": CHAT, "body": "ok"}, displayed_text="wa",
    )
    manager.stage(
        domain="email", action="send_email", policy=PolicyClass.CONFIRM_WRITE,
        payload={"recipient": "x@example.invalid", "body": "ok"}, displayed_text="mail",
    )
    assert manager.resolve_confirmation("ok").status == "ambiguous"
    manager.clear("email")
    current = manager.state.pending.whatsapp
    manager._set_pending("whatsapp", current.model_copy(update={"expires_at": 0}))
    assert manager.resolve_confirmation("ok").status == "expired"
