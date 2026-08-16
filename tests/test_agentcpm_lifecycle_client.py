from __future__ import annotations

import json
import socket
import threading

import pytest

from ralfloop_agent.providers.agentcpm_lifecycle import AgentCpmLifecycleClient, AgentCpmLifecycleError


def one_reply_server(path, reply):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        server.listen(1)
        conn, _ = server.accept()
        with conn:
            request = json.loads(conn.makefile("rb").readline())
            assert request == {"action": "status"}
            conn.sendall(reply)


def test_client_socket_only_validates_response(tmp_path):
    path = tmp_path / "broker.sock"
    reply = b'{"ok":true,"action":"status","active":true,"main_pid":42}\n'
    thread = threading.Thread(target=one_reply_server, args=(path, reply))
    thread.start()
    while not path.exists():
        pass
    assert AgentCpmLifecycleClient(path).status()["main_pid"] == 42
    thread.join()


def test_client_rejects_malformed_response(tmp_path):
    path = tmp_path / "broker.sock"
    thread = threading.Thread(target=one_reply_server, args=(path, b'bad\n'))
    thread.start()
    while not path.exists():
        pass
    with pytest.raises(AgentCpmLifecycleError, match="malformed_response"):
        AgentCpmLifecycleClient(path).status()
    thread.join()
