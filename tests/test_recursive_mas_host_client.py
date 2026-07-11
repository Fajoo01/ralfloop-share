import json
import socket
import threading

from ralfloop_agent.integration.recursive_mas_host_client import (
    PROTOCOL_VERSION,
    RecursiveMASHostClient,
    RecursiveMASHostClientConfig,
    validate_host_request,
)


def test_socket_missing_returns_backend_unavailable(tmp_path):
    client = RecursiveMASHostClient(RecursiveMASHostClientConfig(socket_path=str(tmp_path / "missing.sock"), timeout_sec=0.1))
    result = client.execute({"protocol_version": 1, "request_id": "r1", "goal": "What is 2 + 3?"})
    assert result["ok"] is False
    assert result["status"] == "backend_unavailable"
    assert result["error_type"] == "socket_missing"


def test_forbidden_fields_block_executable_and_model_path():
    result = validate_host_request({"protocol_version": 1, "goal": "x", "executable": "/bin/sh", "model_path": "/m"})
    assert result["ok"] is False
    assert result["status"] == "invalid_request"
    assert result["error_type"] == "forbidden_fields"


def test_protocol_version_and_payload_size():
    assert validate_host_request({"protocol_version": 2, "goal": "x"})["error_type"] == "unsupported_protocol_version"
    assert validate_host_request({"protocol_version": 1, "goal": "x" * 100}, max_request_bytes=20)["error_type"] == "payload_too_large"


def test_human_confirmation_required_is_local(tmp_path):
    client = RecursiveMASHostClient(RecursiveMASHostClientConfig(socket_path=str(tmp_path / "missing.sock")))
    result = client.execute({"protocol_version": 1, "request_id": "r2", "goal": "send email", "requires_human_confirmation": True})
    assert result["status"] == "human_confirmation_required"


def test_client_round_trip_busy(tmp_path):
    sock_path = str(tmp_path / "mas.sock")
    ready = threading.Event()

    def server():
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.bind(sock_path)
            sock.listen(1)
            ready.set()
            conn, _ = sock.accept()
            with conn:
                conn.recv(4096)
                conn.sendall(json.dumps({"protocol_version": PROTOCOL_VERSION, "ok": False, "status": "busy"}).encode() + b"\n")

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    ready.wait(2)
    client = RecursiveMASHostClient(RecursiveMASHostClientConfig(socket_path=sock_path, timeout_sec=2))
    result = client.execute({"protocol_version": 1, "request_id": "r3", "goal": "x"})
    thread.join(2)
    assert result["status"] == "busy"


def test_importing_client_does_not_import_torch(monkeypatch):
    import subprocess
    import sys

    code = "import sys; import ralfloop_agent.integration.recursive_mas_host_client; print('torch' in sys.modules)"
    out = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    assert out == "False"
