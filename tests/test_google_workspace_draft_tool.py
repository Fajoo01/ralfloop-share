from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gmail_draft_tool_test",
    ROOT / "scripts" / "ralf_google_workspace_draft_tool.py",
)
assert SPEC and SPEC.loader
DRAFT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DRAFT)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return json.dumps(self.payload).encode()


def test_create_then_update_reuses_same_draft_id(monkeypatch):
    calls = []
    responses = iter([
        {"id": "draft_1", "message": {"id": "msg_1"}},
        {"id": "draft_1", "message": {"id": "msg_2"}},
    ])

    def fake_urlopen(req, timeout=0):
        calls.append((req.method, req.full_url, req.data))
        return _Response(next(responses))

    monkeypatch.setattr(DRAFT, "_access_token", lambda _email: "token")
    monkeypatch.setattr(DRAFT.urllib.request, "urlopen", fake_urlopen)
    args = {
        "email": "fabio@tiremminnanz.com",
        "to": "fajoo01@gmail.com",
        "subject": "Canary",
        "body": "Versione uno",
    }
    created = DRAFT.upsert_draft(args)
    updated = DRAFT.upsert_draft({**args, "body": "Versione due", "draftId": created["draftId"]})

    assert created["draftId"] == updated["draftId"] == "draft_1"
    assert created["sent"] is updated["sent"] is False
    assert calls[0][0] == "POST"
    assert calls[1][0] == "PUT"
    assert calls[1][1].endswith("/drafts/draft_1")


def test_draft_tool_is_backend_only_and_never_exposes_send(monkeypatch):
    request = json.dumps({
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": DRAFT.TOOL_NAME, "arguments": {
            "email": "fabio@tiremminnanz.com", "to": "fajoo01@gmail.com",
            "subject": "Canary", "body": "Test",
        }},
    }).encode()
    blocked = json.loads(DRAFT.handle_tool_call(request, caller_uid=1001, write_uid=1000))
    assert blocked["error"]["message"] == "gmail_draft_write_requires_backend_uid"

    monkeypatch.setattr(DRAFT, "upsert_draft", lambda _args: {
        "status": "draft_created", "draftId": "draft_1", "sent": False,
    })
    allowed = json.loads(DRAFT.handle_tool_call(request, caller_uid=1000, write_uid=1000))
    structured = allowed["result"]["structuredContent"]
    assert structured["draftId"] == "draft_1"
    assert structured["sent"] is False
    schema = DRAFT.tool_definition()["inputSchema"]
    assert "operation" not in schema["properties"]
    assert "send" not in json.dumps(schema).casefold()
