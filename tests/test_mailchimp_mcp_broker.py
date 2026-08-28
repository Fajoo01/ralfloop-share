from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import time
from unittest.mock import patch

from src.mcp_transport import MCPClientSession, UnixMCPTransport


ROOT = Path(__file__).resolve().parents[1]
BROKER_PATH = ROOT / "scripts/ralf_mailchimp_mcp_broker.py"
SERVER_PATH = ROOT / "scripts/ralf_mailchimp_mcp_server.py"
PRODUCTION_PYTHON = Path(
    "/home/sibilla-cumana/ralfloop_agent_scaffold/.venv/bin/python"
)
SPEC = importlib.util.spec_from_file_location("mailchimp_broker", BROKER_PATH)
assert SPEC and SPEC.loader
BROKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BROKER)


def test_child_uses_exact_configured_python_without_shell(tmp_path):
    stderr = tmp_path / "stderr"
    with stderr.open("w+b") as stream, patch.object(
        BROKER.subprocess, "Popen"
    ) as popen:
        BROKER._start_child("/venv/bin/python", "/release/server.py", stream)

    popen.assert_called_once_with(
        ["/venv/bin/python", "/release/server.py"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stream,
        shell=False,
        start_new_session=True,
    )


def test_path_validators_reject_relative_or_non_python_commands(tmp_path):
    executable = tmp_path / "python"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    not_python = tmp_path / "server"
    not_python.write_text("", encoding="utf-8")

    assert BROKER._validated_executable(str(executable)) == str(executable)
    assert BROKER._validated_server_script(str(SERVER_PATH)) == str(SERVER_PATH)

    for value, validator in (
        ("python", BROKER._validated_executable),
        (str(not_python), BROKER._validated_server_script),
    ):
        try:
            validator(value)
        except Exception as exc:
            assert exc.__class__.__name__ == "ArgumentTypeError"
        else:
            raise AssertionError("unsafe broker path accepted")


def test_production_venv_initializes_server_through_uid_guard(tmp_path):
    assert PRODUCTION_PYTHON.is_file()
    socket_path = tmp_path / "mcp.sock"
    process = subprocess.Popen(
        [
            str(PRODUCTION_PYTHON),
            str(BROKER_PATH),
            "--socket",
            str(socket_path),
            "--allow-uid",
            str(os.getuid()),
            "--python",
            str(PRODUCTION_PYTHON),
            "--command",
            str(SERVER_PATH),
            "--idle-timeout",
            "5",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with MCPClientSession(
            UnixMCPTransport(str(socket_path)), timeout=3
        ) as session:
            tools = session.list_tools()
        assert len(tools) == 9
        assert socket_path.stat().st_mode & 0o777 == 0o600
        assert socket.socket(socket.AF_UNIX).family == socket.AF_UNIX
    finally:
        process.terminate()
        process.wait(timeout=3)


def test_child_diagnostic_never_emits_stderr_content(tmp_path, capsys):
    child = tmp_path / "fail.py"
    child.write_text(
        "import sys\nsys.stderr.write('Authorization: Bearer secret-token')\nraise SystemExit(7)\n",
        encoding="utf-8",
    )
    left, right = socket.socketpair()
    try:
        BROKER._relay(left, str(PRODUCTION_PYTHON), str(child), 2)
    finally:
        left.close()
        right.close()
    diagnostic = capsys.readouterr().err
    assert "returncode=7" in diagnostic
    assert "stderr_bytes=" in diagnostic
    assert "secret-token" not in diagnostic
    assert "Authorization" not in diagnostic
