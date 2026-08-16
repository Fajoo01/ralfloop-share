from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from src.mcp_transport import MCPClientSession, UnixMCPTransport


def test_broker_transparently_relays_mcp_and_closes_child(tmp_path):
    child = tmp_path / "fake-mcp"
    child.write_text("#!/usr/bin/env python3\nimport json,sys\nfor line in sys.stdin:\n r=json.loads(line); i=r.get('id')\n if r.get('method')=='initialize': out={'jsonrpc':'2.0','id':i,'result':{'protocolVersion':'2025-03-26'}}\n elif r.get('method')=='tools/list': out={'jsonrpc':'2.0','id':i,'result':{'tools':[{'name':'manage_email','inputSchema':{'type':'object'}}]}}\n else: continue\n print(json.dumps(out),flush=True)\n")
    child.chmod(0o700)
    sock = tmp_path / "mcp.sock"
    broker = subprocess.Popen([sys.executable, "scripts/ralf_google_workspace_mcp_broker.py",
        "--socket", str(sock), "--allow-uid", str(os.getuid()), "--command", str(child)])
    try:
        deadline = time.monotonic() + 3
        while not sock.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        with MCPClientSession(UnixMCPTransport(str(sock))) as session:
            assert [tool.name for tool in session.list_tools()] == ["manage_email"]
        assert socket.socket(socket.AF_UNIX).family == socket.AF_UNIX
        assert sock.stat().st_mode & 0o777 == 0o660
    finally:
        broker.terminate(); broker.wait(timeout=3)
