from __future__ import annotations

from pathlib import Path
import os
import socket
import subprocess
import sys
import time

from src.arci import READ_TOOL
from src.mcp_transport import MCPClientSession, UnixMCPTransport


def test_arci_broker_relays_strict_server_tool_list(tmp_path):
    socket_path = tmp_path / "mcp.sock"
    server = Path("scripts/ralf_arci_mcp_server.py").resolve()
    broker = subprocess.Popen([
        sys.executable,
        "scripts/ralf_arci_mcp_broker.py",
        "--socket",
        str(socket_path),
        "--command",
        str(server),
        "--idle-timeout",
        "5",
    ])
    try:
        deadline = time.monotonic() + 3
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        with MCPClientSession(
            UnixMCPTransport(str(socket_path)),
            timeout=3,
        ) as session:
            tools = session.list_tools()
        assert [tool.name for tool in tools] == [READ_TOOL]
        assert tools[0].input_schema == {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        }
        assert socket_path.stat().st_mode & 0o777 == 0o600
    finally:
        broker.terminate()
        broker.wait(timeout=3)


def test_arci_systemd_candidate_is_read_only_and_loopback_bounded():
    unit = Path(
        "deploy/systemd/ralf-arci-mcp-broker.service"
    ).read_text(encoding="utf-8")

    assert "RuntimeDirectory=ralf-arci-mcp" in unit
    assert "--allow-uid" not in unit
    assert "User=sibilla-cumana" in unit
    assert "ralf_arci_mcp_broker.py" in unit
    assert "ralf_arci_mcp_server.py" in unit
    assert "ralfloop_agent_scaffold/.venv/bin/python" in unit
    assert "WorkingDirectory=/home/sibilla-cumana/ralfloop-production/current" in unit
    assert "/home/sibilla-cumana/ralfloop-production/current/scripts/ralf_arci_mcp_broker.py" in unit
    assert "/home/sibilla-cumana/ralfloop-production/current/scripts/ralf_arci_mcp_server.py" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "IPAddressDeny=any" in unit
    assert "IPAddressAllow=localhost" in unit


def test_arci_broker_concurrent_clients_avoid_head_of_line_blocking(tmp_path):
    socket_path = tmp_path / "mcp-concurrent.sock"
    server = Path("scripts/ralf_arci_mcp_server.py").resolve()
    env = dict(os.environ)
    env["RALF_MCP_IDLE_TIMEOUT"] = "5"
    env["RALF_MCP_MAX_CLIENTS"] = "2"
    broker = subprocess.Popen([
        sys.executable,
        "scripts/ralf_arci_mcp_broker.py",
        "--socket", str(socket_path),
        "--command", str(server),
    ], env=env)
    blocker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        deadline = time.monotonic() + 3
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        blocker.connect(str(socket_path))
        # The 1.5 s transport timeout is the HOL assertion: the blocker
        # remains connected for the broker's 5 s idle timeout, so a serial
        # broker would time out here before it could serve this client.
        with MCPClientSession(
            UnixMCPTransport(str(socket_path)), timeout=1.5
        ) as session:
            tools = session.list_tools()
        assert [tool.name for tool in tools] == [READ_TOOL]
    finally:
        blocker.close()
        broker.terminate()
        broker.wait(timeout=3)
