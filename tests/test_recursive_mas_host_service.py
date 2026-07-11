import json
import socket
import threading
import time

from ralfloop_agent.integration.recursive_mas_host_service import RecursiveMASUnixServer


class FakeController:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def execute(self, request):
        self.calls.append(request)
        return dict(self.result)


def _start_server(sock_path, controller):
    server = RecursiveMASUnixServer(str(sock_path), controller=controller)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _send(sock_path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(str(sock_path))
        sock.sendall(json.dumps(payload).encode() + b"\n")
        return json.loads(sock.recv(65536).decode())


def test_service_disabled_response_from_controller(tmp_path):
    controller = FakeController({"ok": False, "status": "disabled", "error_type": "backend_disabled", "cleanup_completed": True})
    server, _thread = _start_server(tmp_path / "mas.sock", controller)
    try:
        result = _send(tmp_path / "mas.sock", {"protocol_version": 1, "request_id": "r1", "goal": "What is 2 + 3?"})
    finally:
        server.shutdown()
        server.server_close()
        _thread.join(2)
    assert result["status"] == "disabled"
    assert controller.calls[0]["goal"] == "What is 2 + 3?"


def test_service_success_normalization(tmp_path):
    controller = FakeController({"ok": True, "status": "completed", "answer": "\\boxed{5}", "native_latent_verified": True, "closed_loop_verified": True, "cleanup_completed": True})
    server, _thread = _start_server(tmp_path / "mas.sock", controller)
    try:
        result = _send(tmp_path / "mas.sock", {"protocol_version": 1, "request_id": "r2", "goal": "What is 2 + 3?", "rounds": 1})
    finally:
        server.shutdown()
        server.server_close()
        _thread.join(2)
    assert result["ok"] is True
    assert result["answer"] == "\\boxed{5}"
    assert result["native_latent_verified"] is True


def test_service_rejects_payload_too_large(tmp_path):
    controller = FakeController({"ok": True, "status": "completed"})
    server = RecursiveMASUnixServer(str(tmp_path / "mas.sock"), controller=controller, max_request_bytes=32)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(str(tmp_path / "mas.sock"))
            sock.sendall(b"x" * 80 + b"\n")
            result = json.loads(sock.recv(4096).decode())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
    assert result["status"] == "invalid_request"
    assert result["error_type"] == "payload_too_large"
    assert controller.calls == []


def test_service_removes_socket_on_close(tmp_path):
    server, _thread = _start_server(tmp_path / "mas.sock", FakeController({"ok": False, "status": "disabled"}))
    assert (tmp_path / "mas.sock").exists()
    server.shutdown()
    server.server_close()
    _thread.join(2)
    time.sleep(0.05)
    assert not (tmp_path / "mas.sock").exists()
