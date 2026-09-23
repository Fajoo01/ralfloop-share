from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import websocket

from ralfloop_agent.integration.gpt_browser_cdp import BrowserTarget, CdpError, ChromeCdp
import tools.bottazzi_gpt_session as gpt_session_tool
from tools.bottazzi_gpt_session import (
    _recover_incomplete_mutation,
    _recover_stored_source_home_tab,
    _recover_stored_source_new_tab,
    _requires_mutation_lock,
    _resolve_stored_source,
)

from ralfloop_agent.integration.gpt_session_rollover import (
    ExternalChatAdoptionStore,
    GptSessionError,
    Handoff,
    HandoffStore,
    MutationJournalStore,
    MutationLock,
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


def test_gpt_browser_units_recreate_disposable_cache_after_boot() -> None:
    repo = Path(__file__).resolve().parents[1]
    for relative in (
        "deploy/systemd/bottazzi-gpt-browser.service",
        "deploy/systemd/bottazzi-gpt-browser-login.service",
    ):
        unit = (repo / relative).read_text(encoding="utf-8")
        assert "RuntimeDirectory=bottazzi-gpt-browser-cache" in unit
        assert "RuntimeDirectoryMode=0700" in unit
        assert "StartLimitIntervalSec=60" in unit
        assert "StartLimitBurst=5" in unit
        assert "Environment=TMPDIR=/run/bottazzi-gpt-browser-cache" in unit
        assert "Environment=BOTTAZZI_GPT_CACHE_DIR=/run/bottazzi-gpt-browser-cache" in unit
        assert "ReadWritePaths=/home/bandi/.local/share/bottazzi-gpt-browser /run/bottazzi-gpt-browser-cache" in unit
        assert "/tmp/bottazzi-gpt-browser-cache" not in unit
    script = (repo / "scripts/bottazzi_gpt_browser.sh").read_text(encoding="utf-8")
    assert "${BOTTAZZI_GPT_CACHE_DIR:-/run/bottazzi-gpt-browser-cache}" in script


def test_gpt_browser_script_restores_tabs_without_forcing_persisted_worker_url(tmp_path) -> None:
    repo = Path(__file__).resolve().parents[1]
    fake_chrome = tmp_path / "fake-chrome.sh"
    args_file = tmp_path / "args.txt"
    fake_chrome.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$BOTTAZZI_TEST_ARGS"\n', encoding="utf-8")
    fake_chrome.chmod(0o755)
    state_file = tmp_path / "current.json"
    state_file.write_text(json.dumps({"source_chat_url": "https://chatgpt.com/c/restart-worker"}), encoding="utf-8")
    env = dict(os.environ)
    env.update(
        {
            "BOTTAZZI_GPT_CHROME_BIN": str(fake_chrome),
            "BOTTAZZI_GPT_PROFILE_DIR": str(tmp_path / "profile"),
            "BOTTAZZI_GPT_CACHE_DIR": str(tmp_path / "cache"),
            "BOTTAZZI_GPT_CONFIG_DIR": str(tmp_path / "config"),
            "BOTTAZZI_GPT_STATE_FILE": str(state_file),
            "BOTTAZZI_GPT_HEADLESS": "0",
            "BOTTAZZI_TEST_ARGS": str(args_file),
        }
    )
    subprocess.run([str(repo / "scripts/bottazzi_gpt_browser.sh")], check=True, env=env)
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert "--restore-last-session" in args
    assert "https://chatgpt.com/c/restart-worker" not in args
    assert args[-1] == "https://chatgpt.com/"


def test_mutation_lock_is_nonblocking_and_exclusive(tmp_path) -> None:
    first = MutationLock(tmp_path)
    with first:
        with pytest.raises(GptSessionError, match="mutation_locked"):
            with MutationLock(tmp_path):
                pass
    with MutationLock(tmp_path):
        pass
    assert first.path.stat().st_mode & 0o077 == 0


def test_mutation_journal_round_trip_rejects_overwrite_and_clears(tmp_path) -> None:
    journal = MutationJournalStore(tmp_path)
    created = journal.begin(
        "adopt_external",
        source_target_id="source",
        source_url="https://chatgpt.com/c/source",
        candidate_url="https://chatgpt.com/c/candidate",
    )
    assert created["phase"] == "prepared"
    updated = journal.update(phase="browser_done")
    assert updated["phase"] == "browser_done"
    with pytest.raises(GptSessionError, match="mutation_journal_busy"):
        journal.begin("rollover", source_target_id="source", source_url="https://chatgpt.com/c/source")
    assert journal.path.stat().st_mode & 0o077 == 0
    journal.clear()
    assert journal.load() is None


def test_mutation_lock_policy_covers_all_state_mutators() -> None:
    assert _requires_mutation_lock(SimpleNamespace(command="checkpoint")) is True
    assert _requires_mutation_lock(SimpleNamespace(command="adopt-external", apply=False)) is True
    assert _requires_mutation_lock(SimpleNamespace(command="shepherd", apply=False)) is True
    assert _requires_mutation_lock(SimpleNamespace(command="rotate", apply=False)) is False
    assert _requires_mutation_lock(SimpleNamespace(command="rotate", apply=True)) is True
    assert _requires_mutation_lock(SimpleNamespace(command="status")) is False


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
        self.archived: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.temporary_urls: dict[str, str] = {}

    def targets(self):
        targets = [
            BrowserTarget(self.source_target_id, "page", self.source_url, "worker", f"ws://{self.source_target_id}"),
            BrowserTarget("watcher", "page", "https://chatgpt.com/", "watcher", "ws://watcher"),
        ]
        targets.extend(
            BrowserTarget(target_id, "page", url, "temp", f"ws://{target_id}")
            for target_id, url in self.temporary_urls.items()
            if target_id not in self.closed
        )
        return [target for target in targets if target.target_id not in self.closed]

    def conversation_urls(self, target_id: str, *, reload: bool = True):
        assert target_id == "watcher"
        return list(self.history)

    def chatgpt_ui_state(self, target_id: str):
        if target_id != self.source_target_id:
            return {"authenticated": True, "ready": True, "user_turns": 0}
        return {
            "authenticated": True,
            "ready": True,
            "response_pending": self.busy,
            "response_in_progress": self.busy,
            "composer_chars": 0,
        }

    def create_chatgpt_target(self, *, clear_cache: bool = False, background: bool = False) -> str:
        target_id = f"archive-temp-{len(self.temporary_urls) + 1}"
        self.temporary_urls[target_id] = "https://chatgpt.com/"
        return target_id

    def navigate_chatgpt_conversation(self, target_id: str, url: str):
        self.navigated.append((target_id, url))
        if target_id == self.source_target_id:
            self.source_url = url
        else:
            self.temporary_urls[target_id] = url
        return {"authenticated": True, "ready": True, "user_turns": 2}

    def archive_chatgpt_conversation(
        self,
        target_id: str,
        url: str,
        *,
        allow_absent: bool = False,
        archive_started_hook=None,
    ):
        if archive_started_hook is not None:
            archive_started_hook()
        self.archived.append((target_id, url))
        return {"archived": True, "already_archived": False, "conversation_url": url}

    def close_target(self, target_id: str) -> None:
        self.closed.append(target_id)


def _adoption_args(tmp_path):
    return SimpleNamespace(endpoint="http://127.0.0.1:9238", state_dir=str(tmp_path), scan_interval_seconds=0, apply=True)


class FakeRecoveryCdp:
    def __init__(self, targets: list[BrowserTarget], ui_by_target: dict[str, dict] | None = None) -> None:
        self._targets = list(targets)
        self.ui_by_target = dict(ui_by_target or {})
        self.closed: list[str] = []
        self.archived: list[tuple[str, str]] = []
        self.human_input_targets: list[tuple[str, str]] = []
        self.ghosted: list[tuple[str, str | None]] = []
        self.locked: list[str] = []
        self.unlocked: list[str] = []

    def targets(self):
        return [target for target in self._targets if target.target_id not in self.closed]

    def chatgpt_ui_state(self, target_id: str):
        return dict(self.ui_by_target.get(target_id) or {"authenticated": True, "ready": True, "user_turns": 0})

    def create_chatgpt_target(self, *, clear_cache: bool = False, background: bool = False) -> str:
        target_id = f"archive-temp-{len(self._targets)}"
        self._targets.append(BrowserTarget(target_id, "page", "https://chatgpt.com/", "temp", f"ws://{target_id}"))
        return target_id

    def navigate_chatgpt_conversation(self, target_id: str, url: str):
        self._targets = [
            BrowserTarget(target.target_id, target.target_type, url if target.target_id == target_id else target.url, target.title, target.websocket_url)
            for target in self._targets
        ]
        return {"authenticated": True, "ready": True, "user_turns": 0}

    def archive_chatgpt_conversation(
        self,
        target_id: str,
        url: str,
        *,
        allow_absent: bool = False,
        archive_started_hook=None,
    ):
        if archive_started_hook is not None:
            archive_started_hook()
        self.archived.append((target_id, url))
        return {"archived": True, "already_archived": False, "conversation_url": url}

    def lock_human_input_during_handoff(self, target_id: str):
        self.locked.append(target_id)
        return {"ok": True, "locked": True}

    def unlock_human_input_after_failed_handoff(self, target_id: str):
        self.unlocked.append(target_id)
        return {"ok": True, "locked": False}

    def install_human_input_target(self, target_id: str, conversation_url: str):
        self.human_input_targets.append((target_id, conversation_url))
        return {"ok": True, "human_input_target": True, "conversation_url": conversation_url}

    def mark_chatgpt_ghost_tab(self, target_id: str, *, successor_url: str | None = None, notice: str = ""):
        self.ghosted.append((target_id, successor_url))
        return {"ok": True, "ghost": True, "successor_url": successor_url}

    def close_target(self, target_id: str) -> None:
        self.closed.append(target_id)


def test_incomplete_adoption_recovers_after_browser_navigation(tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source", "https://chatgpt.com/c/source")
    adoption = ExternalChatAdoptionStore(tmp_path)
    adoption.save(
        {
            "seen_conversations": ["https://chatgpt.com/c/source"],
            "watcher_target_id": "watcher",
            "pending_conversation": "https://chatgpt.com/c/from-app",
            "pending_detected_epoch": 123,
            "last_scan_epoch": 123,
        }
    )
    journal = MutationJournalStore(tmp_path)
    journal.begin(
        "adopt_external",
        source_target_id="source",
        source_url="https://chatgpt.com/c/source",
        candidate_url="https://chatgpt.com/c/from-app",
    )
    journal.update(phase="browser_done")
    cdp = FakeRecoveryCdp(
        [
            BrowserTarget("source", "page", "https://chatgpt.com/c/from-app", "worker", "ws://source"),
            BrowserTarget("watcher", "page", "https://chatgpt.com/", "watcher", "ws://watcher"),
        ]
    )

    result = _recover_incomplete_mutation(cdp, handoff, adoption, journal)

    assert result and result["outcome"] == "committed"
    assert handoff.load_current()["source_chat_url"] == "https://chatgpt.com/c/from-app"
    saved = adoption.load()
    assert saved["last_adopted_conversation"] == "https://chatgpt.com/c/from-app"
    assert saved["pending_conversation"] is None
    assert cdp.archived == [("archive-temp-2", "https://chatgpt.com/c/source")]
    assert "archive-temp-2" in cdp.closed
    assert journal.load() is None


def test_incomplete_rollover_recovers_confirmed_successor_and_ghosts_old_source(tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source", "https://chatgpt.com/c/source")
    adoption = ExternalChatAdoptionStore(tmp_path)
    journal = MutationJournalStore(tmp_path)
    journal.begin("rollover", source_target_id="source", source_url="https://chatgpt.com/c/source")
    journal.update(phase="target_created", successor_target_id="successor")
    cdp = FakeRecoveryCdp(
        [
            BrowserTarget("source", "page", "https://chatgpt.com/c/source", "old", "ws://source"),
            BrowserTarget("successor", "page", "https://chatgpt.com/c/successor", "new", "ws://successor"),
        ],
        {"successor": {"ready": True, "user_turns": 1}},
    )

    result = _recover_incomplete_mutation(cdp, handoff, adoption, journal)

    assert result and result["outcome"] == "committed"
    current = handoff.load_current()
    assert current["source_chat"] == "successor"
    assert current["source_chat_url"] == "https://chatgpt.com/c/successor"
    assert cdp.archived == [("source", "https://chatgpt.com/c/source")]
    assert cdp.human_input_targets == [("successor", "https://chatgpt.com/c/successor")]
    assert cdp.ghosted == [("source", "https://chatgpt.com/c/successor")]
    assert cdp.closed == []
    assert result["source_chat_ghosted"] is True
    assert journal.load() is None


class FakeRateLimitedRolloverCdp:
    def __init__(self) -> None:
        self.source = BrowserTarget("source", "page", "https://chatgpt.com/c/source", "worker", "ws://source")
        self.locked = False
        self.unlocked = False

    def lock_human_input_during_handoff(self, target_id: str):
        assert target_id == "source"
        self.locked = True
        return {"ok": True, "locked": True}

    def unlock_human_input_after_failed_handoff(self, target_id: str):
        assert target_id == "source"
        self.unlocked = True
        return {"ok": True, "locked": False}

    def targets(self):
        return [self.source]

    def chatgpt_ui_state(self, target_id: str):
        assert target_id == "source"
        return {
            "ready": True,
            "user_turns": 36,
            "page_age_minutes": 1,
            "consecutive_errors": 0,
            "last_response_latency_ms": 0,
            "current_response_latency_ms": 0,
            "response_pending": False,
            "response_in_progress": False,
            "response_idle_ms": 0,
        }

    def handoff_to_new_chat(self, prompt: str, **kwargs):
        hook = kwargs.get("target_created_hook")
        assert hook is not None
        hook("successor")
        raise CdpError("temporary_access_limited")


def test_rollover_rate_limit_rolls_back_journal_and_remains_operational(monkeypatch, tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source", "https://chatgpt.com/c/source")
    fake = FakeRateLimitedRolloverCdp()
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: fake)
    args = SimpleNamespace(
        endpoint="http://127.0.0.1:9238",
        state_dir=str(tmp_path),
        source_target_id=None,
        max_turns=36,
        max_age_minutes=120,
        max_errors=2,
        max_latency_ms=30000,
        max_stall_ms=60000,
        max_active_stall_ms=600000,
        apply=True,
        submit=True,
    )

    assert gpt_session_tool.cmd_shepherd(args) == 0
    assert fake.locked is True
    assert fake.unlocked is True
    assert MutationJournalStore(tmp_path).load() is None
    assert handoff.load_current()["source_chat_url"] == "https://chatgpt.com/c/source"


def test_incomplete_rollover_rolls_back_unconfirmed_successor(tmp_path) -> None:
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("source", "https://chatgpt.com/c/source")
    adoption = ExternalChatAdoptionStore(tmp_path)
    journal = MutationJournalStore(tmp_path)
    journal.begin("rollover", source_target_id="source", source_url="https://chatgpt.com/c/source")
    journal.update(phase="target_created", successor_target_id="successor")
    cdp = FakeRecoveryCdp(
        [
            BrowserTarget("source", "page", "https://chatgpt.com/c/source", "old", "ws://source"),
            BrowserTarget("successor", "page", "https://chatgpt.com/", "blank", "ws://successor"),
        ]
    )

    result = _recover_incomplete_mutation(cdp, handoff, adoption, journal)

    assert result and result["outcome"] == "rolled_back"
    assert cdp.closed == ["successor"]
    assert handoff.load_current()["source_chat_url"] == "https://chatgpt.com/c/source"
    assert journal.load() is None


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
    assert after_rollover.navigated[0] == ("source-2", "https://chatgpt.com/c/from-app")
    assert after_rollover.archived == [("archive-temp-1", "https://chatgpt.com/c/source-2")]
    assert "archive-temp-1" in after_rollover.closed
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
    assert after_rollover.navigated[0] == ("source-2", "https://chatgpt.com/c/from-app")
    assert after_rollover.archived == [("archive-temp-1", "https://chatgpt.com/c/source-2")]
    assert "archive-temp-1" in after_rollover.closed
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


class HomeRecoveryCdp:
    def __init__(self, url: str) -> None:
        self.tab = BrowserTarget("fresh-target", "page", url, "worker", "ws://fresh")
        self.navigated: list[tuple[str, str]] = []

    def targets(self):
        return [self.tab]

    def chatgpt_ui_state(self, target_id: str):
        assert target_id == self.tab.target_id
        return {"authenticated": True, "ready": True}

    def navigate_chatgpt_conversation(self, target_id: str, url: str):
        assert target_id == self.tab.target_id
        self.navigated.append((target_id, url))
        self.tab = BrowserTarget(target_id, "page", url, "worker", "ws://fresh")
        return {"authenticated": True, "ready": True, "url": url}


class NewTabRecoveryCdp:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self._targets = [
            BrowserTarget("unrelated-1", "page", "https://chatgpt.com/c/other-1", "other-1", "ws://other-1"),
            BrowserTarget("unrelated-2", "page", "https://chatgpt.com/c/other-2", "other-2", "ws://other-2"),
        ]
        self.navigated: list[tuple[str, str]] = []
        self.closed: list[str] = []
        self.created_background: list[bool] = []

    def targets(self):
        return [target for target in self._targets if target.target_id not in self.closed]

    def create_chatgpt_target(self, *, clear_cache: bool = False, background: bool = False) -> str:
        self.created_background.append(background)
        self._targets.append(BrowserTarget("recovery", "page", "https://chatgpt.com/", "recovery", "ws://recovery"))
        return "recovery"

    def navigate_chatgpt_conversation(self, target_id: str, url: str):
        assert target_id == "recovery"
        self.navigated.append((target_id, url))
        self._targets = [
            BrowserTarget(target.target_id, target.target_type, url if target.target_id == target_id else target.url, target.title, target.websocket_url)
            for target in self._targets
        ]
        return {"authenticated": True, "ready": self.ready}

    def close_target(self, target_id: str) -> None:
        self.closed.append(target_id)


def test_stale_source_multi_tab_recovers_in_new_exact_tab_without_hijack(tmp_path) -> None:
    store = HandoffStore(tmp_path)
    store.save(Handoff(goal="x", current_state="y"))
    store.update_source_chat("stale-target", "https://chatgpt.com/c/abc")
    cdp = NewTabRecoveryCdp()
    original = [(tab.target_id, tab.url) for tab in cdp.targets()]
    source, info, error = _resolve_stored_source(cdp.targets(), store)
    assert source is None and error == "stored_source_not_found"

    source, info, error = _recover_stored_source_new_tab(cdp, store, info, error)

    assert error is None
    assert source is not None and source.target_id == "recovery"
    assert info["source_recovered_by_new_tab"] is True
    assert cdp.created_background == [True]
    assert cdp.navigated == [("recovery", "https://chatgpt.com/c/abc")]
    assert [(tab.target_id, tab.url) for tab in cdp.targets() if tab.target_id.startswith("unrelated-")] == original
    assert cdp.closed == []
    current = store.load_current()
    assert current["source_chat"] == "recovery"
    assert current["source_chat_url"] == "https://chatgpt.com/c/abc"


def test_stale_source_multi_tab_recovery_failure_closes_only_created_tab(tmp_path) -> None:
    store = HandoffStore(tmp_path)
    store.save(Handoff(goal="x", current_state="y"))
    store.update_source_chat("stale-target", "https://chatgpt.com/c/abc")
    cdp = NewTabRecoveryCdp(ready=False)
    source, info, error = _resolve_stored_source(cdp.targets(), store)

    recovered, details, recovery_error = _recover_stored_source_new_tab(cdp, store, info, error)

    assert source is None
    assert recovered is None
    assert recovery_error == "stored_source_recovery_failed"
    assert "stored_source_recovery_target_not_ready" in details["source_recovery_error"]
    assert cdp.closed == ["recovery"]
    assert {tab.target_id for tab in cdp.targets()} == {"unrelated-1", "unrelated-2"}
    current = store.load_current()
    assert current["source_chat"] == "stale-target"
    assert current["source_chat_url"] == "https://chatgpt.com/c/abc"


def test_stale_source_home_tab_is_preserved_instead_of_hijacked(tmp_path) -> None:
    store = HandoffStore(tmp_path)
    store.save(Handoff(goal="x", current_state="y"))
    store.update_source_chat("stale-target", "https://chatgpt.com/c/abc")
    cdp = HomeRecoveryCdp("https://chatgpt.com/")
    tabs = cdp.targets()
    source, info, error = _resolve_stored_source(tabs, store)
    assert source is None and error == "stored_source_not_found"

    recovered, info, recovery_error = _recover_stored_source_home_tab(cdp, tabs, store, info, error)

    assert recovered is None
    assert recovery_error == "stored_source_not_found"
    assert info["foreground_preserved"] is True
    assert cdp.navigated == []
    current = store.load_current()
    assert current["source_chat"] == "stale-target"
    assert current["source_chat_url"] == "https://chatgpt.com/c/abc"


def test_stale_source_home_recovery_does_not_hijack_other_conversation(tmp_path) -> None:
    store = HandoffStore(tmp_path)
    store.save(Handoff(goal="x", current_state="y"))
    store.update_source_chat("stale-target", "https://chatgpt.com/c/abc")
    cdp = HomeRecoveryCdp("https://chatgpt.com/c/other")
    tabs = cdp.targets()
    source, info, error = _resolve_stored_source(tabs, store)

    recovered, _, recovery_error = _recover_stored_source_home_tab(cdp, tabs, store, info, error)

    assert source is None
    assert recovered is None
    assert recovery_error == "stored_source_not_found"
    assert cdp.navigated == []
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


def test_create_target_can_stay_in_background(monkeypatch) -> None:
    cdp = ChromeCdp("http://127.0.0.1:1")
    calls: list[tuple[str, dict]] = []

    def browser_call(method, params=None):
        calls.append((method, dict(params or {})))
        return {"targetId": "background-target"}

    monkeypatch.setattr(cdp, "_browser_call", browser_call)
    assert cdp.create_target("about:blank", background=True) == "background-target"
    assert calls == [("Target.createTarget", {"url": "about:blank", "background": True})]


def test_archive_chatgpt_conversation_clicks_only_archive_action(monkeypatch) -> None:
    cdp = ChromeCdp("http://127.0.0.1:1")
    target = BrowserTarget("target", "page", "https://chatgpt.com/c/abc", "chat", "ws://target")
    monkeypatch.setattr(cdp, "_wait_target", lambda target_id, **kwargs: target)
    monkeypatch.setattr(cdp, "chatgpt_ui_state", lambda target_id: {"authenticated": True, "ready": True})
    calls: list[str] = []
    events: list[str] = []

    def page_call(websocket_url, method, params=None):
        assert websocket_url == "ws://target"
        assert method == "Runtime.evaluate"
        expression = (params or {}).get("expression", "")
        calls.append(expression)
        if "menu_opened" in expression:
            events.append("menu")
            return {"result": {"value": json.dumps({"state": "menu_opened"})}}
        if "const labels = new Set" in expression:
            events.append("click")
            assert "'delete'" not in expression.lower()
            assert "'elimina'" not in expression.lower()
            assert "'archive'" in expression.lower()
            assert "'archivia'" in expression.lower()
            return {"result": {"value": json.dumps({"clicked": True})}}
        events.append("verify")
        return {"result": {"value": True}}

    monkeypatch.setattr(cdp, "_page_call", page_call)

    result = cdp.archive_chatgpt_conversation(
        "target",
        "https://chatgpt.com/c/abc",
        wait_timeout_s=0.5,
        archive_started_hook=lambda: events.append("started"),
    )

    assert result["archived"] is True
    assert result["already_archived"] is False
    assert events == ["menu", "started", "click", "verify"]
    assert len(calls) == 3


def test_archive_absent_is_fail_closed_unless_retry_is_explicit(monkeypatch) -> None:
    cdp = ChromeCdp("http://127.0.0.1:1")
    target = BrowserTarget("target", "page", "https://chatgpt.com/c/abc", "chat", "ws://target")
    monkeypatch.setattr(cdp, "_wait_target", lambda target_id, **kwargs: target)
    monkeypatch.setattr(cdp, "chatgpt_ui_state", lambda target_id: {"authenticated": True, "ready": True})
    monkeypatch.setattr(
        cdp,
        "_page_call",
        lambda websocket_url, method, params=None: {"result": {"value": json.dumps({"state": "absent"})}},
    )

    started: list[str] = []
    with pytest.raises(CdpError, match="conversation_archive_source_not_in_history"):
        cdp.archive_chatgpt_conversation(
            "target",
            "https://chatgpt.com/c/abc",
            wait_timeout_s=0.1,
            archive_started_hook=lambda: started.append("started"),
        )
    assert started == []

    result = cdp.archive_chatgpt_conversation(
        "target",
        "https://chatgpt.com/c/abc",
        wait_timeout_s=0.1,
        allow_absent=True,
        archive_started_hook=lambda: started.append("started"),
    )
    assert result["archived"] is True
    assert result["already_archived"] is True
    assert started == []


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


def test_handoff_target_hook_failure_cleans_new_target() -> None:
    cdp = FakeInjectCdp()

    def fail_hook(target_id: str) -> None:
        assert target_id == "new"
        raise RuntimeError("journal_write_failed")

    with pytest.raises(RuntimeError, match="journal_write_failed"):
        cdp.handoff_to_new_chat("handoff", target_created_hook=fail_hook)
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
