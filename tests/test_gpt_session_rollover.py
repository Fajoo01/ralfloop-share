from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import websocket

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, CdpError, ChromeCdp
import tools.bottazzi_gpt_session as gpt_session_tool
from tools.bottazzi_gpt_session import _resolve_stored_source

from ralfloop_agent.integration.gpt_session_rollover import (
    ExternalChatAdoptionStore,
    GptSessionError,
    Handoff,
    HandoffStore,
    RolloverPolicy,
    SessionMetrics,
    evaluate_rollover,
    normalize_chatgpt_conversation_url,
    select_external_conversation,
    session_metrics_from_ui,
    should_defer_latency_rollover,
)


def test_cdp_transport_timeout_is_wrapped(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise websocket.WebSocketTimeoutException("timeout")

    monkeypatch.setattr(websocket, "create_connection", timeout)
    with pytest.raises(CdpError, match="cdp_transport_error:Runtime.evaluate:WebSocketTimeoutException"):
        ChromeCdp("http://127.0.0.1:9238")._rpc("ws://example.invalid", "Runtime.evaluate", {})


def test_rollover_turn_limit() -> None:
    decision = evaluate_rollover(SessionMetrics(turns=36))
    assert decision.rollover is True
    assert "turn_limit" in decision.reasons


def test_phase_boundary_only_after_session_is_mature() -> None:
    assert evaluate_rollover(SessionMetrics(turns=4, phase_boundary=True)).rollover is False
    assert evaluate_rollover(SessionMetrics(turns=18, phase_boundary=True)).rollover is True


def test_policy_can_be_tuned() -> None:
    decision = evaluate_rollover(
        SessionMetrics(turns=9, age_minutes=11),
        RolloverPolicy(max_turns=10, max_age_minutes=10),
    )
    assert decision.rollover is True
    assert decision.reasons == ("age_limit",)


def test_live_ui_metrics_use_current_or_last_latency() -> None:
    metrics = session_metrics_from_ui(
        {
            "user_turns": 7,
            "page_age_minutes": 12,
            "consecutive_errors": 1,
            "last_response_latency_ms": 9000,
            "current_response_latency_ms": 31000,
        }
    )
    assert metrics.turns == 7
    assert metrics.age_minutes == 12
    assert metrics.consecutive_errors == 1
    assert metrics.last_response_latency_ms == 31000
    decision = evaluate_rollover(metrics)
    assert decision.reasons == ("latency_limit",)


def test_live_ui_consecutive_errors_trigger_rollover() -> None:
    metrics = session_metrics_from_ui(
        {
            "user_turns": 3,
            "page_age_minutes": 2,
            "consecutive_errors": 2,
            "last_response_latency_ms": 0,
            "current_response_latency_ms": 0,
        }
    )
    decision = evaluate_rollover(metrics)
    assert decision.rollover is True
    assert decision.reasons == ("error_limit",)


def test_latency_only_rollover_defers_active_response_until_hard_stall() -> None:
    reasons = ("latency_limit",)
    assert should_defer_latency_rollover(reasons, response_pending=True, response_in_progress=True, response_idle_ms=599_999) is True
    assert should_defer_latency_rollover(reasons, response_pending=True, response_in_progress=True, response_idle_ms=600_000) is False
    assert should_defer_latency_rollover(reasons, response_pending=True, response_in_progress=False, response_idle_ms=59_999) is True
    assert should_defer_latency_rollover(reasons, response_pending=True, response_in_progress=False, response_idle_ms=60_000) is False
    assert should_defer_latency_rollover(reasons, response_pending=False, response_in_progress=False, response_idle_ms=0) is False
    assert should_defer_latency_rollover(("error_limit", "latency_limit"), response_pending=True, response_in_progress=True, response_idle_ms=1_000) is False


def test_handoff_round_trip_and_prompt(tmp_path) -> None:
    store = HandoffStore(tmp_path)
    archive = store.save(
        Handoff(
            goal="continuare il task",
            current_state="test verdi",
            repo={"branch": "feat/test", "commit": "abc123"},
            constraints=["non pushare su origin"],
            completed=["creato worktree"],
            tests=["pytest ok"],
            action_receipts=[{"action": "issue GitHub", "status": "done", "ref": "#45"}],
            important_files=["a.py"],
            do_not_touch=["produzione"],
            open_problems=["login una tantum"],
            next_action="aprire nuova chat",
        )
    )
    assert archive.exists()
    assert store.current_path.exists()
    assert archive.stat().st_mode & 0o077 == 0
    assert store.current_path.stat().st_mode & 0o077 == 0

    prompt = store.render_prompt()
    assert "GOAL" in prompt
    assert "continuare il task" in prompt
    assert "non pushare su origin" in prompt
    assert "#45" in prompt

    payload = json.loads(store.current_path.read_text())
    assert payload["schema_version"] == "bottazzi_gpt_handoff_v1"
    store.update_source_chat("worker-target", "https://chatgpt.com/c/worker")
    current = store.load_current()
    assert current["source_chat"] == "worker-target"
    assert current["source_chat_url"] == "https://chatgpt.com/c/worker"


def test_checkpoint_schema_round_trips_runtime_worker_metadata(tmp_path) -> None:
    store = HandoffStore(tmp_path)
    store.save(Handoff(goal="x", current_state="y"))
    store.update_source_chat("worker-target", "https://chatgpt.com/c/worker")
    payload = store.load_current()
    payload["updated_at"] = "2026-09-23T11:49:00+00:00"

    handoff = Handoff(**payload)
    store.save(handoff)

    current = store.load_current()
    assert current["source_chat"] == "worker-target"
    assert current["source_chat_url"] == "https://chatgpt.com/c/worker"
    assert current["updated_at"] == "2026-09-23T11:49:00+00:00"


def test_external_conversation_url_is_canonical_and_query_free() -> None:
    assert normalize_chatgpt_conversation_url("https://chatgpt.com/c/abc-123?messageId=x") == "https://chatgpt.com/c/abc-123"
    assert normalize_chatgpt_conversation_url("https://chatgpt.com/c/abc-123/") == "https://chatgpt.com/c/abc-123"
    assert normalize_chatgpt_conversation_url("https://chatgpt.com/g/g-p-demo/c/abc-123") == "https://chatgpt.com/c/abc-123"
    assert normalize_chatgpt_conversation_url("https://example.com/c/abc-123") is None
    assert normalize_chatgpt_conversation_url("https://chatgpt.com/g/gpt") is None


def test_external_conversation_selection_excludes_seen_open_and_source() -> None:
    urls = [
        "https://chatgpt.com/c/seen",
        "https://chatgpt.com/c/open",
        "https://chatgpt.com/c/from-app?messageId=finalAgentTurnStart",
        "https://chatgpt.com/c/source",
    ]
    assert select_external_conversation(
        urls,
        seen_urls=["https://chatgpt.com/c/seen"],
        open_urls=["https://chatgpt.com/c/open"],
        source_url="https://chatgpt.com/c/source",
    ) == "https://chatgpt.com/c/from-app"


def test_external_conversation_selection_rejects_older_than_source() -> None:
    assert select_external_conversation(
        [
            "https://chatgpt.com/c/source",
            "https://chatgpt.com/c/old-source",
        ],
        seen_urls=[],
        open_urls=[],
        source_url="https://chatgpt.com/c/source",
    ) is None


def test_external_conversation_selection_requires_source_in_history() -> None:
    assert select_external_conversation(
        ["https://chatgpt.com/c/from-app"],
        seen_urls=[],
        open_urls=[],
        source_url="https://chatgpt.com/c/source",
    ) is None


def test_external_adoption_store_round_trip_is_bounded(tmp_path) -> None:
    store = ExternalChatAdoptionStore(tmp_path)
    payload = store.load()
    payload.update(
        {
            "seen_conversations": [
                "https://chatgpt.com/c/one?messageId=x",
                "https://chatgpt.com/c/one",
                "https://example.com/c/nope",
            ],
            "watcher_target_id": "watcher",
            "last_scan_epoch": 123,
        }
    )
    store.save(payload)
    saved = store.load()
    assert saved["seen_conversations"] == ["https://chatgpt.com/c/one"]
    assert saved["watcher_target_id"] == "watcher"
    assert saved["pending_conversation"] is None
    assert saved["pending_detected_epoch"] == 0
    assert saved["unvalidated_candidates"] == []
    assert store.path.stat().st_mode & 0o077 == 0


class FakeAdoptionCdp:
    def __init__(self, source_target_id: str, source_url: str, history: list[str], *, busy: bool = False) -> None:
        self.source_target_id = source_target_id
        self.source_url = source_url
        self.history = history
        self.busy = busy
        self.navigated: list[tuple[str, str]] = []

    def targets(self):
        return [
            BrowserTarget(self.source_target_id, "page", self.source_url, "worker", f"ws://{self.source_target_id}"),
            BrowserTarget("watcher", "page", "https://chatgpt.com/", "watcher", "ws://watcher"),
        ]

    def conversation_urls(self, target_id: str, *, reload: bool = True):
        assert target_id == "watcher"
        return list(self.history)

    def chatgpt_ui_state(self, target_id: str):
        assert target_id == self.source_target_id
        return {
            "ready": True,
            "response_pending": self.busy,
            "response_in_progress": self.busy,
            "composer_chars": 0,
        }

    def navigate_chatgpt_conversation(self, target_id: str, url: str):
        self.navigated.append((target_id, url))
        return {"ready": True, "user_turns": 2}


def _adoption_args(tmp_path):
    return SimpleNamespace(endpoint="http://127.0.0.1:9238", state_dir=str(tmp_path), scan_interval_seconds=0, apply=True)


def test_external_candidate_survives_worker_rollover(monkeypatch, tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source-1", "https://chatgpt.com/c/source-1")
    adoption = ExternalChatAdoptionStore(tmp_path)
    adoption.save(
        {
            "seen_conversations": ["https://chatgpt.com/c/source-1"],
            "watcher_target_id": "watcher",
            "last_adopted_conversation": None,
            "last_scan_epoch": 0,
        }
    )

    busy = FakeAdoptionCdp(
        "source-1",
        "https://chatgpt.com/c/source-1",
        ["https://chatgpt.com/c/from-app", "https://chatgpt.com/c/source-1"],
        busy=True,
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: busy)
    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0
    pending = adoption.load()
    assert pending["pending_conversation"] == "https://chatgpt.com/c/from-app"
    assert pending["last_adopted_conversation"] is None

    handoff.update_source_chat("source-2", "https://chatgpt.com/c/source-2")
    after_rollover = FakeAdoptionCdp(
        "source-2",
        "https://chatgpt.com/c/source-2",
        [
            "https://chatgpt.com/c/source-2",
            "https://chatgpt.com/c/from-app",
            "https://chatgpt.com/c/source-1",
        ],
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: after_rollover)
    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0

    saved = adoption.load()
    assert after_rollover.navigated == [("source-2", "https://chatgpt.com/c/from-app")]
    assert saved["last_adopted_conversation"] == "https://chatgpt.com/c/from-app"
    assert saved["pending_conversation"] is None
    assert saved["pending_detected_epoch"] == 0
    assert handoff.load_current()["source_chat_url"] == "https://chatgpt.com/c/from-app"


def test_source_missing_from_history_does_not_consume_unseen_chat(monkeypatch, tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source", "https://chatgpt.com/c/source")
    adoption = ExternalChatAdoptionStore(tmp_path)
    adoption.save(
        {
            "seen_conversations": ["https://chatgpt.com/c/old"],
            "watcher_target_id": "watcher",
            "last_adopted_conversation": None,
            "last_scan_epoch": 0,
        }
    )
    fake = FakeAdoptionCdp(
        "source",
        "https://chatgpt.com/c/source",
        ["https://chatgpt.com/c/from-app", "https://chatgpt.com/c/old"],
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: fake)

    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0

    saved = adoption.load()
    assert saved["seen_conversations"] == ["https://chatgpt.com/c/old"]
    assert saved["pending_conversation"] is None
    assert saved["unvalidated_candidates"] == [
        {
            "conversation_url": "https://chatgpt.com/c/from-app",
            "source_url": "https://chatgpt.com/c/source",
            "detected_epoch": saved["unvalidated_candidates"][0]["detected_epoch"],
        }
    ]
    assert saved["unvalidated_candidates"][0]["detected_epoch"] > 0
    assert fake.navigated == []


def test_unvalidated_candidate_survives_rollover_and_validates_against_old_source(monkeypatch, tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source-1", "https://chatgpt.com/c/source-1")
    adoption = ExternalChatAdoptionStore(tmp_path)
    adoption.save(
        {
            "seen_conversations": ["https://chatgpt.com/c/old"],
            "watcher_target_id": "watcher",
            "last_adopted_conversation": None,
            "last_scan_epoch": 0,
        }
    )

    source_missing = FakeAdoptionCdp(
        "source-1",
        "https://chatgpt.com/c/source-1",
        ["https://chatgpt.com/c/from-app", "https://chatgpt.com/c/old"],
        busy=True,
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: source_missing)
    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0
    staged = adoption.load()
    assert staged["pending_conversation"] is None
    assert staged["unvalidated_candidates"][0]["conversation_url"] == "https://chatgpt.com/c/from-app"
    assert staged["unvalidated_candidates"][0]["source_url"] == "https://chatgpt.com/c/source-1"

    handoff.update_source_chat("source-2", "https://chatgpt.com/c/source-2")
    after_rollover = FakeAdoptionCdp(
        "source-2",
        "https://chatgpt.com/c/source-2",
        [
            "https://chatgpt.com/c/source-2",
            "https://chatgpt.com/c/from-app",
            "https://chatgpt.com/c/source-1",
            "https://chatgpt.com/c/old",
        ],
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: after_rollover)
    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0

    saved = adoption.load()
    assert after_rollover.navigated == [("source-2", "https://chatgpt.com/c/from-app")]
    assert saved["last_adopted_conversation"] == "https://chatgpt.com/c/from-app"
    assert saved["unvalidated_candidates"] == []
    assert handoff.load_current()["source_chat_url"] == "https://chatgpt.com/c/from-app"


def test_unvalidated_late_old_history_entry_is_rejected(monkeypatch, tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source-1", "https://chatgpt.com/c/source-1")
    adoption = ExternalChatAdoptionStore(tmp_path)
    adoption.save(
        {
            "seen_conversations": ["https://chatgpt.com/c/old"],
            "watcher_target_id": "watcher",
            "last_adopted_conversation": None,
            "last_scan_epoch": 0,
        }
    )

    source_missing = FakeAdoptionCdp(
        "source-1",
        "https://chatgpt.com/c/source-1",
        ["https://chatgpt.com/c/late-old", "https://chatgpt.com/c/old"],
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: source_missing)
    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0

    handoff.update_source_chat("source-2", "https://chatgpt.com/c/source-2")
    after_rollover = FakeAdoptionCdp(
        "source-2",
        "https://chatgpt.com/c/source-2",
        [
            "https://chatgpt.com/c/source-2",
            "https://chatgpt.com/c/source-1",
            "https://chatgpt.com/c/late-old",
            "https://chatgpt.com/c/old",
        ],
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: after_rollover)
    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0

    saved = adoption.load()
    assert after_rollover.navigated == []
    assert "https://chatgpt.com/c/late-old" in saved["seen_conversations"]
    assert saved["unvalidated_candidates"] == []


def test_unvalidated_candidate_is_not_consumed_before_anchor_appears(monkeypatch, tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source-1", "https://chatgpt.com/c/source-1")
    adoption = ExternalChatAdoptionStore(tmp_path)
    adoption.save(
        {
            "seen_conversations": ["https://chatgpt.com/c/old"],
            "watcher_target_id": "watcher",
            "last_adopted_conversation": None,
            "last_scan_epoch": 0,
        }
    )

    source_missing = FakeAdoptionCdp(
        "source-1",
        "https://chatgpt.com/c/source-1",
        ["https://chatgpt.com/c/from-app", "https://chatgpt.com/c/old"],
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: source_missing)
    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0

    handoff.update_source_chat("source-2", "https://chatgpt.com/c/source-2")
    anchor_still_missing = FakeAdoptionCdp(
        "source-2",
        "https://chatgpt.com/c/source-2",
        ["https://chatgpt.com/c/source-2", "https://chatgpt.com/c/from-app", "https://chatgpt.com/c/old"],
    )
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: anchor_still_missing)
    assert gpt_session_tool.cmd_adopt_external(_adoption_args(tmp_path)) == 0

    saved = adoption.load()
    assert anchor_still_missing.navigated == []
    assert "https://chatgpt.com/c/from-app" not in saved["seen_conversations"]
    assert saved["unvalidated_candidates"][0]["conversation_url"] == "https://chatgpt.com/c/from-app"


def test_stored_source_recovers_after_cdp_target_change(tmp_path) -> None:
    store = HandoffStore(tmp_path)
    store.save(Handoff(goal="x", current_state="y"))
    store.update_source_chat("stale-target", "https://chatgpt.com/c/abc")
    tabs = [
        BrowserTarget("other", "page", "https://chatgpt.com/c/other", "other", "ws://other"),
        BrowserTarget("fresh-target", "page", "https://chatgpt.com/g/g-p-demo/c/abc", "worker", "ws://fresh"),
    ]

    source, info, error = _resolve_stored_source(tabs, store)

    assert error is None
    assert source is not None and source.target_id == "fresh-target"
    assert info["source_recovered"] is True
    current = store.load_current()
    assert current["source_chat"] == "fresh-target"
    assert current["source_chat_url"] == "https://chatgpt.com/c/abc"


def test_stored_source_url_recovery_fails_closed_when_duplicated(tmp_path) -> None:
    store = HandoffStore(tmp_path)
    store.save(Handoff(goal="x", current_state="y"))
    store.update_source_chat("stale-target", "https://chatgpt.com/c/abc")
    tabs = [
        BrowserTarget("one", "page", "https://chatgpt.com/c/abc", "one", "ws://one"),
        BrowserTarget("two", "page", "https://chatgpt.com/g/g-p-demo/c/abc", "two", "ws://two"),
    ]

    source, info, error = _resolve_stored_source(tabs, store)

    assert source is None
    assert error == "stored_source_url_ambiguous"
    assert info["match_count"] == 2
    assert store.load_current()["source_chat"] == "stale-target"


def test_handoff_rejects_secret_named_fields(tmp_path) -> None:
    handoff = Handoff(goal="x", current_state="y")
    handoff.action_receipts = [{"action": "x", "token": "should-never-be-stored"}]
    with pytest.raises(GptSessionError, match="secret_field_not_allowed"):
        HandoffStore(tmp_path).save(handoff)


def test_chatgpt_target_detection() -> None:
    assert BrowserTarget("1", "page", "https://chatgpt.com/c/abc", "x").is_chatgpt
    assert BrowserTarget("2", "page", "https://foo.chatgpt.com/", "x").is_chatgpt
    assert not BrowserTarget("3", "page", "https://example.com/?next=chatgpt.com", "x").is_chatgpt


def test_conversation_navigation_waits_for_initial_blank(monkeypatch) -> None:
    cdp = ChromeCdp("http://127.0.0.1:1")
    sequence = iter(
        [
            BrowserTarget("target", "page", "about:blank", "blank", "ws://target"),
            BrowserTarget("target", "page", "https://chatgpt.com/", "home", "ws://target"),
            BrowserTarget("target", "page", "https://chatgpt.com/c/abc", "chat", "ws://target"),
        ]
    )
    settled = BrowserTarget("target", "page", "https://chatgpt.com/c/abc", "chat", "ws://target")

    def wait_target(target_id, *, attempts=20):
        assert target_id == "target"
        return next(sequence, settled)

    monkeypatch.setattr(cdp, "_wait_target", wait_target)
    monkeypatch.setattr(cdp, "_page_call", lambda *args, **kwargs: {})
    monkeypatch.setattr(cdp, "chatgpt_ui_state", lambda target_id: {"ready": True, "target_id": target_id})

    result = cdp.navigate_chatgpt_conversation("target", "https://chatgpt.com/c/abc", wait_timeout_s=1.0)
    assert result["ready"] is True


class FakeCdp(ChromeCdp):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:1")
        self.closed: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.new_id = "new"

    def targets(self):
        return [
            BrowserTarget("old", "page", "https://chatgpt.com/c/old", "old", "ws://old"),
            BrowserTarget(self.new_id, "page", "about:blank", "blank", "ws://new"),
        ]

    def create_target(self, url: str) -> str:
        assert url == "about:blank"
        return self.new_id

    def close_target(self, target_id: str) -> None:
        self.closed.append(target_id)

    def _page_call(self, websocket_url: str, method: str, params=None):
        self.calls.append((websocket_url, method))
        return {}


def test_rotate_closes_only_old_chatgpt_and_clears_cache() -> None:
    cdp = FakeCdp()
    result = cdp.rotate_chatgpt_tab()
    assert cdp.closed == ["old"]
    assert result["new_target_id"] == "new"
    assert result["cache_cleared"] is True
    assert result["server_chat_deleted"] is False
    assert ("ws://new", "Network.clearBrowserCache") in cdp.calls
    assert ("ws://new", "Page.navigate") in cdp.calls

class FakeInjectCdp(FakeCdp):
    def __init__(self) -> None:
        super().__init__()
        self.sent = False

    def chatgpt_ui_state(self, target_id=None):
        return {"ready": True, "target_id": target_id or self.new_id, "user_turns": 1 if self.sent else 0}

    def _wait_target(self, target_id, *, attempts=20):
        return BrowserTarget(target_id, "page", "https://chatgpt.com/", "new", "ws://new")

    def _page_call(self, websocket_url, method, params=None):
        self.calls.append((websocket_url, method))
        if method == "Runtime.evaluate":
            if "send-button" in (params or {}).get("expression", ""):
                self.sent = True
                return {"result": {"value": json.dumps({"clicked": True})}}
            return {"result": {"value": json.dumps({"ok": True})}}
        if method == "Input.dispatchKeyEvent" and (params or {}).get("type") == "keyUp":
            self.sent = True
        return {}


def test_inject_prompt_can_submit_with_button() -> None:
    cdp = FakeInjectCdp()
    result = cdp.inject_prompt("handoff", target_id="new", submit=True)
    assert result["injected"] is True
    assert result["submitted"] is True
    assert result["submit_method"] == "button_js"
    assert [method for _, method in cdp.calls].count("Input.insertText") == 1
    assert [method for _, method in cdp.calls].count("Input.dispatchMouseEvent") == 0
    assert [method for _, method in cdp.calls].count("Input.dispatchKeyEvent") == 0


class FailingHandoffCdp(FakeCdp):
    def inject_prompt(self, prompt, *, target_id=None, submit=False, wait_timeout_s=20.0):
        raise CdpError("chatgpt_not_ready:interaction_required")


def test_handoff_failure_keeps_old_chatgpt_tab_open() -> None:
    cdp = FailingHandoffCdp()
    with pytest.raises(CdpError, match="interaction_required"):
        cdp.handoff_to_new_chat("handoff")
    assert cdp.closed == ["new"]

class RedirectingInjectCdp(FakeInjectCdp):
    def _wait_target(self, target_id, *, attempts=20):
        url = "https://accounts.google.com/" if self.sent else "https://chatgpt.com/"
        return BrowserTarget(target_id, "page", url, "redirect", "ws://new")


def test_inject_prompt_rejects_auth_redirect() -> None:
    cdp = RedirectingInjectCdp()
    with pytest.raises(CdpError, match="submit_interaction_required"):
        cdp.inject_prompt("handoff", target_id="new", submit=True)

class MultiTabHandoffCdp(FakeInjectCdp):
    def targets(self):
        return [
            BrowserTarget("old", "page", "https://chatgpt.com/c/old", "old", "ws://old"),
            BrowserTarget("other", "page", "https://chatgpt.com/c/other", "other", "ws://other"),
            BrowserTarget(self.new_id, "page", "about:blank", "blank", "ws://new"),
        ]


def test_handoff_closes_only_selected_source_tab() -> None:
    cdp = MultiTabHandoffCdp()
    result = cdp.handoff_to_new_chat("handoff", source_target_id="old", submit=True)
    assert result["closed_target_ids"] == ["old"]
    assert cdp.closed == ["old"]


def test_handoff_can_defer_source_close_until_state_is_persisted() -> None:
    cdp = MultiTabHandoffCdp()
    result = cdp.handoff_to_new_chat(
        "handoff",
        source_target_id="old",
        submit=True,
        close_source=False,
    )
    assert result["new_target_id"] == "new"
    assert result["closed_target_ids"] == []
    assert cdp.closed == []


def test_handoff_requires_source_when_multiple_chatgpt_tabs() -> None:
    cdp = MultiTabHandoffCdp()
    with pytest.raises(CdpError, match="handoff_source_ambiguous"):
        cdp.handoff_to_new_chat("handoff", submit=True)
    assert cdp.closed == []
