from __future__ import annotations

import hashlib

from ralfloop_agent.unified_assistant import runtime
from ralfloop_agent.unified_assistant.browser_mcp_adapter import BrowserMCPApprovalProvider


class FakeRuntimeBrowserProvider(BrowserMCPApprovalProvider):
    def __init__(self) -> None:
        super().__init__(session_factory=lambda: None)
        self.apply_calls = 0
        self.before = '- button "Conferma" [ref=e7]'
        self.after = '- status "Salvato" [ref=e9]'

    def snapshot(self):
        text = self.after if self.apply_calls else self.before
        return {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}

    def apply(self, scope):
        self.apply_calls += 1
        return {
            "ok": True, "logical_action": scope["logical_action"],
            "calls": [{"tool": "browser_click", "arguments": {"target": scope["target"]}}],
            "writes": 1,
        }


def test_runtime_browser_approval_executes_once(monkeypatch, tmp_path):
    provider = FakeRuntimeBrowserProvider()
    monkeypatch.setenv("RALFLOOP_UNIFIED_ASSISTANT", "1")
    monkeypatch.setenv("RALFLOOP_BROWSER_INTERACT_LIVE", "1")
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "11")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "22")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approvals.sqlite"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_UNIFIED_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(runtime, "BrowserMCPApprovalProvider", lambda: provider)
    context = {
        "source": "telegram_natural", "telegram_user_id": 11,
        "telegram_chat_id": 22, "telegram_message_id": 1,
        "telegram_chat_type": "private",
    }

    preview = runtime.run_unified_telegram("browser clicca e7", context)
    assert preview["metadata"]["status"] == "draft_pending_approval"
    assert preview["metadata"]["approval_request_id"]
    assert provider.apply_calls == 0

    approved = runtime.run_unified_telegram(
        "ok", {**context, "telegram_message_id": 2}
    )
    assert approved["metadata"]["status"] == "EXECUTED_VERIFIED"
    assert approved["metadata"]["result"]["post_snapshot"]
    assert provider.apply_calls == 1

    replay = runtime.run_unified_telegram(
        "ok", {**context, "telegram_message_id": 3}
    )
    assert replay["metadata"]["status"] == "no_pending_action"
    assert provider.apply_calls == 1
