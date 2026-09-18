from __future__ import annotations

import json
import stat

import pytest

from ralfloop_agent.cli.session_store import SessionStore, SessionStoreError, default_sessions_dir
from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.conversation import (
    ConversationManager,
    SessionConversationAdapter,
)


def test_session_persists_atomically_with_private_permissions(tmp_path):
    root = tmp_path / "state" / "ralf" / "sessions"
    store = SessionStore(root)
    record = store.create(cwd=str(tmp_path), model="local-model")
    record["history"] = [
        {"role": "user", "content": "ciao"},
        {"role": "assistant", "content": "ok"},
    ]
    store.save(record)

    path = root / f"{record['session_id']}.json"
    assert store.load(record["session_id"])["history"][1]["content"] == "ok"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(root.glob("*.tmp")) == []
    assert json.loads(path.read_text(encoding="utf-8"))["session_id"] == record["session_id"]


def test_session_latest_resume_and_delete(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    first = store.create(cwd="/one")
    second = store.create(cwd="/two")
    second["updated_at"] = "9999-12-31T00:00:00+00:00"
    store.save(second)

    assert store.latest()["session_id"] == second["session_id"]
    assert [row["session_id"] for row in store.list()] == [second["session_id"], first["session_id"]]
    store.delete(second["session_id"])
    with pytest.raises(SessionStoreError, match="session_not_found"):
        store.load(second["session_id"])


def test_session_id_cannot_escape_state_directory(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    with pytest.raises(SessionStoreError, match="invalid_session_id"):
        store.load("../../outside")


def test_session_rejects_secret_metadata(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    record = store.create(cwd=str(tmp_path))
    record["metadata"] = {"api_token": "not-stored"}
    with pytest.raises(SessionStoreError, match="secret_metadata_not_allowed"):
        store.save(record)


def test_session_redacts_secret_values_from_history(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    record = store.create(cwd=str(tmp_path))
    record["history"] = [
        {"role": "user", "content": "token=abc password:xyz Authorization: Bearer-value"},
        {"role": "assistant", "content": "Bearer qwerty"},
    ]
    store.save(record)

    raw = (store.root / f"{record['session_id']}.json").read_text(encoding="utf-8")
    assert "abc" not in raw
    assert "xyz" not in raw
    assert "qwerty" not in raw
    assert raw.count("[REDACTED]") >= 2


def test_session_redacts_natural_language_keys_private_keys_and_high_entropy(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    record = store.create(cwd=str(tmp_path))
    private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nabc123\n-----END OPENSSH PRIVATE KEY-----"
    record["history"] = [
        {"role": "user", "content": "API key is sk-abcdefgh12345678\npassword is correct horse battery staple"},
        {"role": "assistant", "content": private_key + " A234567890bcdefghijklmnopqrstuv"},
    ]

    store.save(record)

    raw = (store.root / f"{record['session_id']}.json").read_text(encoding="utf-8")
    assert "sk-abcdefgh" not in raw
    assert "horse battery staple" not in raw
    assert "OPENSSH PRIVATE KEY" not in raw
    assert "A234567890bcdefghijklmnopqrstuv" not in raw


def test_session_rejects_nonessential_metadata_even_with_benign_key(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    record = store.create(cwd=str(tmp_path))
    record["metadata"] = {"note": "sk-abcdefgh12345678"}

    with pytest.raises(SessionStoreError, match="unsupported_session_metadata"):
        store.save(record)


def test_session_removes_terminal_control_characters_from_history(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    record = store.create(cwd=str(tmp_path))
    record["history"] = [{"role": "assistant", "content": "safe\x1b]2;owned\x07text\x9b"}]

    store.save(record)

    raw = (store.root / f"{record['session_id']}.json").read_bytes()
    assert b"\x1b" not in raw
    assert b"u001b" not in raw
    assert b"\x07" not in raw
    assert b"\xc2\x9b" not in raw


def test_default_state_directory_honors_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    assert default_sessions_dir() == tmp_path / "ralf" / "sessions"


def test_email_reply_seed_with_thread_context_is_persistable(tmp_path):
    store = SessionStore(tmp_path / "sessions")
    record = store.create(cwd=str(tmp_path))
    manager = ConversationManager()
    manager.stage(
        domain="email",
        action="reply_email",
        policy=PolicyClass.CONFIRM_WRITE,
        payload={
            "recipient": "caterina@example.org",
            "working_seed": {
                "source_email": {
                    "thread_context": [{
                        "sender": "Caterina",
                        "body": "Invito al festival",
                    }],
                },
            },
        },
        displayed_text="Preview",
    )
    adapter = SessionConversationAdapter(store)

    adapter.save(record["session_id"], manager)

    assert adapter.load(record["session_id"]).state == manager.state


def test_session_introspection_archive_preserves_rolled_out_turns(tmp_path):
    sessions = tmp_path / "sessions"
    archive = tmp_path / "introspection"
    store = SessionStore(sessions, introspection_root=archive)
    record = store.create(cwd=str(tmp_path), model="local-model")
    record["history"] = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    store.save(record)
    record["history"] = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    store.save(record)
    record["history"] = [
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u3"},
        {"role": "assistant", "content": "a3"},
    ]
    store.save(record)

    path = archive / f"{record['session_id']}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [row["content"] for row in payload["history"]] == [
        "u1", "a1", "u2", "a2", "u3", "a3",
    ]
    assert stat.S_IMODE(archive.stat().st_mode) == 0o750
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
