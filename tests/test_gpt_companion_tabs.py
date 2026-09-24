import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import tools.bottazzi_gpt_session as gpt_session_tool
from ralfloop_agent.integration.gpt_session_rollover import Handoff, HandoffStore
from tools.bottazzi_gpt_session import _companion_tabs


class FakeStore:
    current_path = Path("/virtual/current.json")

    def __init__(self, worker_id: str):
        self.worker_id = worker_id

    def load_current(self):
        return {"source_chat": self.worker_id}


class ExistingPath:
    def exists(self) -> bool:
        return True


class FakeCdp:
    def __init__(self, tabs, focus=None, ui=None, records=None, records_by_target=None, record_errors=None):
        self._tabs = tabs
        self._focus = focus or {}
        self._ui = ui or {}
        self._records = records or []
        self._records_by_target = records_by_target or {}
        self._record_errors = set(record_errors or [])

    def targets(self):
        return self._tabs

    def chatgpt_focus_state(self, target_id):
        return self._focus.get(target_id, {})

    def chatgpt_ui_state(self, target_id):
        return self._ui.get(target_id, {})

    def conversation_records(self, target_id, *, reload=False, wait_timeout_s=3.0):
        if target_id in self._record_errors:
            raise gpt_session_tool.CdpError("history_probe_failed")
        return self._records_by_target.get(target_id, self._records)


def tab(target_id: str, conversation_id: str, title: str):
    return SimpleNamespace(
        target_id=target_id,
        target_type="page",
        is_chatgpt=True,
        title=title,
        url=f"https://chatgpt.com/c/{conversation_id}",
    )


def store(worker_id: str):
    value = FakeStore(worker_id)
    value.current_path = ExistingPath()
    return value


def test_companion_tabs_deduplicates_targets_and_merges_real_activity():
    cdp = FakeCdp(
        [
            tab("busy-copy", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "same conversation"),
            tab("worker-copy", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "same conversation"),
            tab("focused", "11111111-2222-3333-4444-555555555555", "foreground"),
            tab("open", "99999999-8888-7777-6666-555555555555", "idle"),
        ],
        focus={"focused": {"focused": True}},
        ui={"busy-copy": {"response_in_progress": True}},
    )

    rows = _companion_tabs(cdp, store("worker-copy"))

    assert len(rows) == 3
    merged = next(row for row in rows if row["conversation_url"].endswith("eeeeeeeeeeee"))
    assert merged["target_id"] == "worker-copy"
    assert merged["target_count"] == 2
    assert set(merged["target_ids"]) == {"busy-copy", "worker-copy"}
    assert merged["worker"] is True
    assert merged["busy"] is True
    assert merged["active"] is True
    assert merged["state"] == "responding"

    foreground = next(row for row in rows if row["target_id"] == "focused")
    assert foreground["active"] is True
    assert foreground["state"] == "focused"

    idle = next(row for row in rows if row["target_id"] == "open")
    assert idle["active"] is False
    assert idle["state"] == "open"


def test_companion_tabs_merges_account_history_without_marking_recent_as_active():
    open_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    recent_id = "11111111-2222-3333-4444-555555555555"
    cdp = FakeCdp(
        [tab("worker", open_id, "browser title")],
        records=[
            {"url": f"https://chatgpt.com/c/{recent_id}", "title": "Backtest formiche volumi"},
            {"url": f"https://chatgpt.com/c/{open_id}", "title": "Ripresa rollover GPT"},
        ],
    )

    rows = _companion_tabs(cdp, store("worker"))

    assert len(rows) == 2
    worker = next(row for row in rows if row["conversation_url"].endswith("eeeeeeeeeeee"))
    assert worker["title"] == "Ripresa rollover GPT"
    assert worker["worker"] is True
    assert worker["local_open"] is True
    assert worker["account_recent"] is True
    recent = next(row for row in rows if row["conversation_url"].endswith("555555555555"))
    assert recent["title"] == "Backtest formiche volumi"
    assert recent["target_id"] == ""
    assert recent["target_count"] == 0
    assert recent["state"] == "recent"
    assert recent["active"] is False
    assert recent["local_open"] is False
    assert recent["account_recent"] is True


def test_companion_tabs_falls_back_when_worker_history_probe_fails():
    worker_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    healthy_id = "99999999-8888-7777-6666-555555555555"
    recent_id = "11111111-2222-3333-4444-555555555555"
    cdp = FakeCdp(
        [tab("stuck", worker_id, "worker"), tab("healthy", healthy_id, "healthy")],
        records_by_target={
            "healthy": [{"url": f"https://chatgpt.com/c/{recent_id}", "title": "Backtest formiche volumi"}],
        },
        record_errors={"stuck"},
    )

    rows = _companion_tabs(cdp, store("stuck"))

    recent = next(row for row in rows if row["conversation_url"].endswith("555555555555"))
    assert recent["title"] == "Backtest formiche volumi"
    assert recent["state"] == "recent"
    assert recent["active"] is False
    assert recent["local_open"] is False
    assert recent["account_recent"] is True


def test_companion_switch_blocker_detects_busy_and_unsent_targets():
    class SwitchCdp:
        def __init__(self, states):
            self.states = states

        def chatgpt_companion_state(self, target_id):
            return self.states[target_id]

    busy_tabs = [tab("busy", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee", "busy")]
    assert gpt_session_tool._companion_switch_blocker(SwitchCdp({"busy": {"busy": True}}), busy_tabs) == "worker_response_active"

    draft_tabs = [tab("draft", "99999999-8888-7777-6666-555555555555", "draft")]
    assert gpt_session_tool._companion_switch_blocker(SwitchCdp({"draft": {"busy": False, "composer_chars": 4}}), draft_tabs) == "unsent_composer_text"


def test_companion_tabs_only_marks_logical_conversation_ghost_when_all_targets_are_ghosts():
    conversation_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    cdp = FakeCdp(
        [tab("ghost", conversation_id, "old copy"), tab("live", conversation_id, "live copy")],
        focus={"ghost": {"ghost": True}, "live": {"ghost": False}},
    )

    rows = _companion_tabs(cdp, store(""))

    assert len(rows) == 1
    assert rows[0]["target_id"] == "live"
    assert rows[0]["ghost"] is False
    assert rows[0]["target_count"] == 2


def test_companion_send_cannot_reassign_worker_implicitly(monkeypatch, tmp_path, capsys):
    worker_url = "https://chatgpt.com/c/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    other_url = "https://chatgpt.com/c/11111111-2222-3333-4444-555555555555"
    handoff = HandoffStore(tmp_path)
    handoff.save(Handoff(goal="x", current_state="y"))
    handoff.update_source_chat("worker", worker_url)

    class SendCdp:
        def __init__(self):
            self.activated = []
            self.installed = []
            self.queued = []

        def targets(self):
            return [SimpleNamespace(target_id="other", target_type="page", is_chatgpt=True, url=other_url)]

        def chatgpt_focus_state(self, target_id):
            return {"ghost": False}

        def activate_target(self, target_id):
            self.activated.append(target_id)

        def install_human_input_target(self, target_id, url):
            self.installed.append((target_id, url))

        def queue_human_message(self, target_id, url, text):
            self.queued.append((target_id, url, text))
            return {"queued": True}

    fake = SendCdp()
    monkeypatch.setattr(gpt_session_tool, "ChromeCdp", lambda endpoint: fake)
    monkeypatch.setattr(sys, "stdin", io.StringIO("ciao"))
    args = SimpleNamespace(endpoint="http://127.0.0.1:9238", state_dir=str(tmp_path), target_id="other")

    assert gpt_session_tool.cmd_companion_send(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "worker_assignment_mismatch"
    assert fake.activated == []
    assert fake.installed == []
    assert fake.queued == []
    assert handoff.load_current()["source_chat"] == "worker"
