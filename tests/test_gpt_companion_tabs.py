from pathlib import Path
from types import SimpleNamespace

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
    def __init__(self, tabs, focus=None, ui=None):
        self._tabs = tabs
        self._focus = focus or {}
        self._ui = ui or {}

    def targets(self):
        return self._tabs

    def chatgpt_focus_state(self, target_id):
        return self._focus.get(target_id, {})

    def chatgpt_ui_state(self, target_id):
        return self._ui.get(target_id, {})


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
