from __future__ import annotations

from ralfloop_agent.unified_assistant import runtime
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


ITEM = "a" * 32


class FakeJellyfinProvider:
    def __init__(self) -> None:
        self.apply_calls = 0
        self.identity = {
            "ok": True, "status": "FOUND", "item_id": ITEM,
            "name": "Test Film", "year": 2024, "provider_ids": {},
        }

    def read_identity(self, item_id):
        assert item_id == ITEM
        return dict(self.identity)

    def search(self, item_id, *, name="", year=None):
        assert item_id == ITEM
        return {
            "ok": True, "status": "FOUND", "item_id": ITEM,
            "items": [{"name": "Test Film", "year": 2024,
                       "provider_ids": {"Tmdb": "123"}}],
        }

    def apply(self, scope):
        self.apply_calls += 1
        self.identity = {
            **self.identity,
            "provider_ids": {"Tmdb": "123"},
        }
        return {"ok": True, "status": "APPLIED"}


def test_planner_extracts_exact_jellyfin_identity_scope():
    plan = UnifiedPlanner(UnifiedRegistryFacade()).plan(
        f"applica identità Jellyfin item_id={ITEM} provider=Tmdb provider_id=123"
    )
    assignment = plan.assignments[0]
    assert assignment.skill == "jellyfin.apply_identity"
    assert assignment.policy.value == "PROTECTED"
    assert assignment.arguments == {
        "item_id": ITEM,
        "provider": "Tmdb",
        "provider_id": "123",
    }


def test_runtime_jellyfin_approval_executes_exact_identity_once(monkeypatch, tmp_path):
    provider = FakeJellyfinProvider()
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_JELLYFIN_IDENTITY_WRITE_LIVE", "1")
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "11")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "22")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approvals.sqlite"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(runtime, "JellyfinIdentityMCPProvider", lambda: provider)

    context = {
        "source": "telegram_natural",
        "telegram_user_id": 11,
        "telegram_chat_id": 22,
        "telegram_message_id": 1,
        "telegram_chat_type": "private",
    }
    request = (
        f"applica identità Jellyfin item_id={ITEM} "
        "provider=Tmdb provider_id=123"
    )

    preview = runtime.run_unified_telegram(request, context)
    assert preview["metadata"]["status"] == "protected_approval_required"
    assert preview["approval_required"] is True
    assert preview["metadata"]["approval_request_id"]
    assert provider.apply_calls == 0

    approved = runtime.run_unified_telegram(
        "ok", {**context, "telegram_message_id": 2}
    )
    assert approved["metadata"]["status"] == "EXECUTED_VERIFIED"
    assert approved["metadata"]["result"]["provider_id"] == "123"
    assert provider.apply_calls == 1

    replay = runtime.run_unified_telegram(
        "ok", {**context, "telegram_message_id": 3}
    )
    assert replay["metadata"]["status"] == "no_pending_action"
    assert provider.apply_calls == 1
