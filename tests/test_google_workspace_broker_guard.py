from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location(
    "ralf_google_workspace_mcp_broker_guard_test",
    SCRIPTS / "ralf_google_workspace_mcp_broker.py",
)
assert SPEC and SPEC.loader
BROKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BROKER)


def _request(name: str, arguments: dict) -> bytes:
    return json.dumps({
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode()


@pytest.mark.parametrize("operation", ["send", "reply", "replyAll", "forward"])
def test_direct_manage_email_writes_require_backend_uid(operation):
    blocked = BROKER._guard_client_line(
        _request("manage_email", {"operation": operation}),
        caller_uid=1001, write_uid=1000,
    )
    assert blocked is not None
    payload = json.loads(blocked)
    assert payload["error"]["message"] == "outbound_email_write_blocked"
    assert payload["error"]["data"]["reason"] == "bot_tazzi_approval_path_required"


def test_manage_email_read_remains_available_to_shared_peer():
    assert BROKER._guard_client_line(
        _request("manage_email", {"operation": "search", "query": "test"}),
        caller_uid=1001, write_uid=1000,
    ) is None


def test_backend_uid_can_reach_existing_bound_approval_path():
    assert BROKER._guard_client_line(
        _request("manage_email", {"operation": "send"}),
        caller_uid=1000, write_uid=1000,
    ) is None


def test_missing_write_uid_is_fail_closed():
    assert BROKER._guard_client_line(
        _request("manage_email", {"operation": "send"}),
        caller_uid=1000, write_uid=None,
    ) is not None


def test_scratchpad_email_send_is_blocked_but_draft_is_allowed():
    blocked = BROKER._guard_client_line(
        _request("manage_scratchpad", {"operation": "send", "target": "email"}),
        caller_uid=1001, write_uid=1000,
    )
    assert blocked is not None
    assert BROKER._guard_client_line(
        _request("manage_scratchpad", {"operation": "send", "target": "email_draft"}),
        caller_uid=1001, write_uid=1000,
    ) is None


def test_queue_wrapped_email_send_is_blocked():
    arguments = {
        "mode": "queue",
        "operations": [
            {"tool": "manage_email", "args": {"operation": "search"}},
            {"tool": "manage_email", "args": {"operation": "send"}},
        ],
    }
    assert BROKER._guard_client_line(
        _request("bulk_operations", arguments), caller_uid=1001, write_uid=1000,
    ) is not None


def test_batch_wrapped_email_send_is_blocked():
    arguments = {"mode": "batch", "tool": "manage_email", "operation": "send"}
    assert BROKER._guard_client_line(
        _request("bulk_operations", arguments), caller_uid=1001, write_uid=1000,
    ) is not None


def test_relay_blocks_direct_send_before_downstream(tmp_path):
    received = tmp_path / "received.jsonl"
    child = tmp_path / "fake-mcp"
    child.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib,sys\n"
        f"p=pathlib.Path({str(received)!r})\n"
        "for line in sys.stdin: p.open('ab').write(line.encode() if isinstance(line,str) else line)\n"
    )
    child.chmod(0o700)
    sock_path = tmp_path / "mcp.sock"
    broker = subprocess.Popen([
        sys.executable, str(SCRIPTS / "ralf_google_workspace_mcp_broker.py"),
        "--socket", str(sock_path), "--allow-uid", str(os.getuid()),
        "--write-uid", str(os.getuid() + 1), "--command", str(child),
    ])
    try:
        deadline = time.monotonic() + 3
        while not sock_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(sock_path))
            client.sendall(_request("manage_email", {"operation": "send"}) + b"\n")
            response = json.loads(client.recv(4096))
        assert response["error"]["message"] == "outbound_email_write_blocked"
        time.sleep(0.05)
        assert not received.exists() or received.read_bytes() == b""
    finally:
        broker.terminate()
        broker.wait(timeout=3)
