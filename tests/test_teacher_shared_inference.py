from __future__ import annotations

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from ralfloop_agent.teacher.inference import (
    SharedTeacherInferenceEngine,
    TeacherInferenceClient,
)
from src.mcp_transport import (
    MCPClientSession,
    UnixMCPTransport,
)
from src.teacher import ALL_TOOLS


class FakeSharedBackend:
    instances = []

    def __init__(self):
        self.ensure_calls = []
        self.calls = []
        self.closed = False
        self.__class__.instances.append(self)

    def ensure_session(self, session_id):
        self.ensure_calls.append(session_id)

    def __call__(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        return {"response": user_prompt}

    def close(self):
        self.closed = True


def test_shared_engine_uses_one_backend_for_multiple_requests():
    FakeSharedBackend.instances.clear()

    engine = SharedTeacherInferenceEngine(
        backend_factory=FakeSharedBackend,
    )

    first = engine.infer("system", "studente A")
    second = engine.infer("system", "studente B")

    assert first["response"] == "studente A"
    assert second["response"] == "studente B"

    assert len(FakeSharedBackend.instances) == 1

    backend = FakeSharedBackend.instances[0]

    assert backend.ensure_calls == [
        "teacher-shared-engine"
    ]
    assert len(backend.calls) == 2

    engine.close()
    assert backend.closed is True


def test_inference_client_session_methods_do_not_control_gpu(tmp_path):
    client = TeacherInferenceClient(
        tmp_path / "not-used.sock"
    )

    result_a = client.ensure_session("student-session-a")
    result_b = client.ensure_session("student-session-b")

    assert result_a["status"] == "shared_engine"
    assert result_b["status"] == "shared_engine"

    assert client.release_session("student-session-a") is None
    assert client.release_session("student-session-b") is None


def test_teacher_broker_accepts_second_client_while_first_is_open(
    tmp_path,
):
    socket_path = tmp_path / "teacher-mcp.sock"
    inference_path = tmp_path / "inference.sock"
    db_path = tmp_path / "teacher.sqlite3"

    server_script = Path(
        "scripts/ralf_teacher_mcp_server.py"
    ).resolve()

    broker = subprocess.Popen(
        [
            sys.executable,
            "scripts/ralf_teacher_mcp_broker.py",
            "--socket",
            str(socket_path),
            "--allow-uid",
            str(os.getuid()),
            "--command",
            str(server_script),
            "--idle-timeout",
            "10",
            "--max-clients",
            "4",
        ],
        env={
            **os.environ,
            "RALF_TEACHER_DB": str(db_path),
            # Non serve un daemon reale per tools/list.
            "RALF_TEACHER_INFERENCE_SOCKET": str(
                inference_path
            ),
        },
    )

    first = socket.socket(
        socket.AF_UNIX,
        socket.SOCK_STREAM,
    )

    try:
        deadline = time.monotonic() + 3

        while (
            not socket_path.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        first.connect(str(socket_path))

        # Il primo client resta volutamente aperto e inattivo.
        # Il secondo deve comunque poter usare MCP.
        with MCPClientSession(
            UnixMCPTransport(str(socket_path)),
            timeout=3,
        ) as second:
            tools = second.list_tools()

        assert {tool.name for tool in tools} == set(ALL_TOOLS)

    finally:
        first.close()
        broker.terminate()
        broker.wait(timeout=5)
