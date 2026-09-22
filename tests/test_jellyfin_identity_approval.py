from __future__ import annotations

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.conversation import ConversationManager
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.jellyfin_identity_write import (
    JELLYFIN_APPLY_ACTION,
    UnifiedJellyfinApprovalCoordinator,
    UnifiedJellyfinApprovalExecutor,
    prepare_jellyfin_identity_payload,
)


ITEM = "a" * 32


class FakeJellyfinProvider:
    def __init__(self) -> None:
        self.identity = {
            "ok": True, "status": "FOUND", "item_id": ITEM,
            "name": "Test Film", "year": 2024, "provider_ids": {},
        }
        self.apply_calls = 0
        self.break_readback = False

    def read_identity(self, item_id: str):
        assert item_id == ITEM
        return dict(self.identity)

    def search(self, item_id: str, *, name: str = "", year=None):
        assert item_id == ITEM
        return {
            "ok": True, "status": "FOUND", "item_id": ITEM,
            "items": [{
                "name": "Test Film", "year": 2024,
                "provider_ids": {"Tmdb": "123", "Imdb": "tt456"},
            }],
        }

    def apply(self, scope):
        self.apply_calls += 1
        if not self.break_readback:
            self.identity = {
                **self.identity,
                "provider_ids": {"Tmdb": "123", "Imdb": "tt456"},
            }
        return {"ok": True, "status": "APPLIED"}


def policy(tmp_path):
    return DomainApprovalPolicy(
        enabled=True,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        db_path=str(tmp_path / "approvals.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )


def approved_pending(tmp_path, provider):
    payload = prepare_jellyfin_identity_payload(
        provider,
        {"item_id": ITEM, "provider": "Tmdb", "provider_id": "123"},
    )
    manager = ConversationManager()
    pending = manager.stage(
        domain="jellyfin", action=JELLYFIN_APPLY_ACTION,
        policy=PolicyClass.PROTECTED, payload=payload, displayed_text="approve",
    )
    store = DomainApprovalStore(policy=policy(tmp_path))
    coordinator = UnifiedJellyfinApprovalCoordinator(store, policy=policy(tmp_path))
    requested = coordinator.request(pending, requested_by="test")
    assert requested["status"] == "pending"
    pending = manager.attach_approval_request(
        domain="jellyfin", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest,
        approval_ref=requested["request_id"],
        created_at=requested["created_at"], expires_at=requested["expires_at"],
    )
    decision = coordinator.approve(
        pending, telegram_user_id=11, telegram_chat_id=22,
        telegram_message_id=33, chat_type="private",
    )
    assert decision["status"] == "approved"
    pending = manager.bind_approval(
        domain="jellyfin", pending_id=pending.pending_id,
        payload_digest=pending.payload_digest,
        approval_ref=requested["request_id"],
    )
    return store, pending


def test_prepare_binds_before_state_and_exact_candidate():
    provider = FakeJellyfinProvider()
    payload = prepare_jellyfin_identity_payload(
        provider, {"item_id": ITEM, "provider": "tmdb", "provider_id": "123"},
    )
    assert payload["provider"] == "Tmdb"
    assert payload["provider_id"] == "123"
    assert payload["before_identity"]["provider_ids"] == {}
    assert len(payload["before_identity_sha256"]) == 64
    assert len(payload["candidate_sha256"]) == 64


def test_executor_requires_approval_before_any_write(tmp_path):
    provider = FakeJellyfinProvider()
    manager = ConversationManager()
    payload = prepare_jellyfin_identity_payload(
        provider, {"item_id": ITEM, "provider": "Tmdb", "provider_id": "123"},
    )
    pending = manager.stage(
        domain="jellyfin", action=JELLYFIN_APPLY_ACTION,
        policy=PolicyClass.PROTECTED, payload=payload, displayed_text="approve",
    )
    store = DomainApprovalStore(policy=policy(tmp_path))
    result = UnifiedJellyfinApprovalExecutor(store, provider).execute(pending)
    assert result["status"] == "APPROVAL_REQUIRED"
    assert provider.apply_calls == 0


def test_approved_exact_identity_executes_once_and_readback_verifies(tmp_path):
    provider = FakeJellyfinProvider()
    store, pending = approved_pending(tmp_path, provider)
    executor = UnifiedJellyfinApprovalExecutor(store, provider)

    first = executor.execute(pending)
    second = executor.execute(pending)

    assert first["status"] == "EXECUTED_VERIFIED"
    assert first["provider_id"] == "123"
    assert provider.apply_calls == 1
    assert second["status"] == "already_executed"
    assert provider.apply_calls == 1


def test_changed_identity_invalidates_approval_before_write(tmp_path):
    provider = FakeJellyfinProvider()
    store, pending = approved_pending(tmp_path, provider)
    provider.identity = {**provider.identity, "provider_ids": {"Imdb": "other"}}

    result = UnifiedJellyfinApprovalExecutor(store, provider).execute(pending)

    assert result["status"] == "DRAFT_CHANGED"
    assert provider.apply_calls == 0
    assert store.get_request(pending.approval_ref)["status"] == "stale"


def test_write_disabled_stops_before_claim_and_apply(tmp_path):
    provider = FakeJellyfinProvider()
    store, pending = approved_pending(tmp_path, provider)

    result = UnifiedJellyfinApprovalExecutor(
        store, provider, write_enabled=False,
    ).execute(pending)

    assert result["status"] == "jellyfin_write_disabled"
    assert result["provider_call_attempted"] is False
    assert provider.apply_calls == 0
    assert store.get_request(pending.approval_ref)["status"] == "approved"


def test_failed_post_write_readback_is_uncertain_and_not_retryable(tmp_path):
    provider = FakeJellyfinProvider()
    store, pending = approved_pending(tmp_path, provider)
    provider.break_readback = True
    executor = UnifiedJellyfinApprovalExecutor(store, provider)

    first = executor.execute(pending)
    second = executor.execute(pending)

    assert first["status"] == "EXECUTION_UNCERTAIN"
    assert first["retry_allowed"] is False
    assert provider.apply_calls == 1
    assert store.get_request(pending.approval_ref)["status"] == "execution_failed"
    assert second["status"] == "EXECUTION_UNCERTAIN"
    assert provider.apply_calls == 1
