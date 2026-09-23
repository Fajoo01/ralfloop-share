from __future__ import annotations

import json

import pytest

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, ChromeCdp
from ralfloop_agent.integration.gpt_session_rollover import (
    GptSessionError,
    Handoff,
    HandoffStore,
    RolloverPolicy,
    SessionMetrics,
    evaluate_rollover,
)


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


def test_handoff_rejects_secret_named_fields(tmp_path) -> None:
    handoff = Handoff(goal="x", current_state="y")
    handoff.action_receipts = [{"action": "x", "token": "should-never-be-stored"}]
    with pytest.raises(GptSessionError, match="secret_field_not_allowed"):
        HandoffStore(tmp_path).save(handoff)


def test_chatgpt_target_detection() -> None:
    assert BrowserTarget("1", "page", "https://chatgpt.com/c/abc", "x").is_chatgpt
    assert BrowserTarget("2", "page", "https://foo.chatgpt.com/", "x").is_chatgpt
    assert not BrowserTarget("3", "page", "https://example.com/?next=chatgpt.com", "x").is_chatgpt


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
