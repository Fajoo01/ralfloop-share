from __future__ import annotations

import json

import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

import ralfloop_agent.teacher.inference as inference_module
from ralfloop_agent.teacher.inference import (
    OllamaCpuTeacherFallback,
    PooledOllamaCpuTeacherFallback,
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



class FailingSharedBackend:
    instances = []

    def __init__(self):
        self.closed = False
        self.__class__.instances.append(self)

    def ensure_session(self, session_id):
        raise RuntimeError("gpu_busy")

    def close(self):
        self.closed = True


class FakeCpuFallback:
    def __init__(self):
        self.calls = 0

    @staticmethod
    def allowed(user_prompt):
        payload = json.loads(user_prompt)
        return payload.get("pedagogy", {}).get("model_path") != "deep" and bool(
            payload.get("deterministic_evidence", {}).get("concept_evidence")
        )

    def infer(self, system_prompt, user_prompt):
        self.calls += 1
        return {"response": "fallback grounded", "_inference_path": "test_cpu"}

    def stream(self, system_prompt, user_prompt):
        result = self.infer(system_prompt, user_prompt)
        yield {"type": "delta", "text": result["response"]}
        yield {"type": "done", "result": result, "metadata": {"fallback": "cpu"}, "model_path": "cpu_fallback"}

    def close(self):
        return None


def _guarded_prompt(path="fast"):
    return json.dumps({
        "pedagogy": {"model_path": path},
        "deterministic_evidence": {"concept_evidence": {"found": True, "evidence": "guard"}},
        "request": {"question": "test"},
    })


def test_shared_engine_falls_back_only_for_grounded_fast_requests():
    FailingSharedBackend.instances.clear()
    engine = SharedTeacherInferenceEngine(
        backend_factory=FailingSharedBackend,
        fallback_factory=FakeCpuFallback,
    )
    result = engine.infer("system", _guarded_prompt())
    assert result["response"] == "fallback grounded"
    assert FailingSharedBackend.instances[-1].closed is True
    with pytest.raises(RuntimeError, match="gpu_busy"):
        engine.infer("system", _guarded_prompt("deep"))
    engine.close()


def test_shared_engine_stream_uses_cpu_fallback_after_gpu_failure():
    engine = SharedTeacherInferenceEngine(
        backend_factory=FailingSharedBackend,
        fallback_factory=FakeCpuFallback,
    )
    events = list(engine.stream("system", _guarded_prompt()))
    assert [event["type"] for event in events] == ["delta", "done"]
    assert events[-1]["model_path"] == "cpu_fallback"
    engine.close()


def test_shared_engine_prefers_grounded_cpu_fast_lane(monkeypatch):
    monkeypatch.setenv("RALF_TEACHER_CPU_FAST_LANE", "1")
    FakeSharedBackend.instances.clear()
    engine = SharedTeacherInferenceEngine(
        backend_factory=FakeSharedBackend,
        fallback_factory=FakeCpuFallback,
    )
    result = engine.infer("system", _guarded_prompt())
    assert result["response"] == "fallback grounded"
    assert result["_inference_path"] == "test_cpu"
    assert FakeSharedBackend.instances == []
    assert engine._fallback.calls == 1
    engine.close()


def test_shared_engine_fast_lane_failure_falls_through_to_primary(monkeypatch):
    class BrokenCpuFallback(FakeCpuFallback):
        def infer(self, system_prompt, user_prompt):
            self.calls += 1
            raise RuntimeError("cpu_unavailable")

    monkeypatch.setenv("RALF_TEACHER_CPU_FAST_LANE", "1")
    FakeSharedBackend.instances.clear()
    engine = SharedTeacherInferenceEngine(
        backend_factory=FakeSharedBackend,
        fallback_factory=BrokenCpuFallback,
    )
    result = engine.infer("system", _guarded_prompt())
    assert result["response"] == _guarded_prompt()
    assert len(FakeSharedBackend.instances) == 1
    assert engine._fallback.calls == 1
    engine.close()


def test_shared_engine_stream_prefers_grounded_cpu_fast_lane(monkeypatch):
    monkeypatch.setenv("RALF_TEACHER_CPU_FAST_LANE", "1")
    FakeSharedBackend.instances.clear()
    engine = SharedTeacherInferenceEngine(
        backend_factory=FakeSharedBackend,
        fallback_factory=FakeCpuFallback,
    )
    events = list(engine.stream("system", _guarded_prompt()))
    assert [event["type"] for event in events] == ["delta", "done"]
    assert events[-1]["model_path"] == "cpu_fallback"
    assert FakeSharedBackend.instances == []
    engine.close()


def test_ollama_cpu_fallback_is_loopback_cpu_only_and_schema_bound(monkeypatch):
    captured = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1):
            return json.dumps({"message": {"content": json.dumps({"response": "Risposta curata"})}}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(inference_module, "urlopen", fake_urlopen)
    fallback = OllamaCpuTeacherFallback(timeout=30)
    result = fallback.infer("system", _guarded_prompt())
    assert result["response"] == "Risposta curata"
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert captured["payload"]["model"] == "gemma3:4b"
    assert captured["payload"]["options"]["num_gpu"] == 0
    assert captured["payload"]["format"]["required"] == ["response"]
    assert captured["payload"]["keep_alive"] == "30s"


def test_ollama_cpu_fallback_rejects_external_endpoint_and_ungrounded_prompt():
    with pytest.raises(RuntimeError, match="url_denied"):
        OllamaCpuTeacherFallback(base_url="http://example.com:11434")
    fallback = OllamaCpuTeacherFallback(timeout=30)
    assert fallback.allowed(json.dumps({"pedagogy": {"model_path": "fast"}})) is False


class FakePoolSession:
    def __init__(self, *, fail_acquire=False):
        self.fail_acquire = fail_acquire
        self.calls = []
        self.closed = False

    def call_tool(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "pool.acquire":
            if self.fail_acquire:
                raise RuntimeError("pool_busy")
            return {"structuredContent": {
                "ok": True,
                "node": "temistocle",
                "endpoint": "http://10.44.1.20:19106",
                "lease": "lease-1",
            }}
        if name == "pool.release":
            return {"structuredContent": {"ok": True}}
        raise AssertionError(name)

    def close(self):
        self.closed = True


def test_pooled_ollama_fallback_uses_bounded_lease_and_releases(monkeypatch, tmp_path):
    captured = {}
    pool = FakePoolSession()

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1):
            return json.dumps({"message": {"content": json.dumps({"response": "Dal pool"})}}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data)
        return Response()

    monkeypatch.setattr(inference_module, "urlopen", fake_urlopen)
    config = tmp_path / "pool.conf"
    config.write_text("temistocle 10.44.1.20 19106 1\n", encoding="utf-8")
    fallback = PooledOllamaCpuTeacherFallback(
        config_path=config,
        pool_session_factory=lambda: pool,
        timeout=30,
    )
    result = fallback.infer("system", _guarded_prompt())
    assert result["response"] == "Dal pool"
    assert result["_inference_path"] == "ollama_cpu_pool:temistocle"
    assert captured["url"] == "http://10.44.1.20:19106/api/chat"
    assert captured["payload"]["model"] == "gemma3:4b"
    assert pool.calls[0] == ("pool.acquire", {"model": "gemma3:4b"})
    assert pool.calls[-1][0] == "pool.release"
    assert pool.calls[-1][1]["lease"] == "lease-1"
    assert pool.calls[-1][1]["latency_ms"] >= 0
    fallback.close()
    assert pool.closed is True


def test_pooled_ollama_fallback_uses_local_gemma_when_pool_unavailable(monkeypatch, tmp_path):
    captured = {}
    pool = FakePoolSession(fail_acquire=True)

    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1):
            return json.dumps({"message": {"content": json.dumps({"response": "Locale"})}}).encode()

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        return Response()

    monkeypatch.setattr(inference_module, "urlopen", fake_urlopen)
    config = tmp_path / "pool.conf"
    config.write_text("temistocle 10.44.1.20 19106 1\n", encoding="utf-8")
    fallback = PooledOllamaCpuTeacherFallback(
        config_path=config,
        pool_session_factory=lambda: pool,
        timeout=30,
    )
    result = fallback.infer("system", _guarded_prompt())
    assert result["response"] == "Locale"
    assert result["_inference_path"] == "ollama_cpu"
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    assert pool.calls == [("pool.acquire", {"model": "gemma3:4b"})]


def test_shared_engine_selects_pool_only_with_explicit_config(monkeypatch, tmp_path):
    config = tmp_path / "pool.conf"
    config.write_text("sibilla 127.0.0.1 19106 1\n", encoding="utf-8")
    monkeypatch.setenv("RALF_TEACHER_CPU_FALLBACK", "1")
    monkeypatch.setenv("RALF_TEACHER_MODEL_POOL_CONFIG", str(config))
    engine = SharedTeacherInferenceEngine(backend_factory=FakeSharedBackend)
    assert isinstance(engine._fallback, PooledOllamaCpuTeacherFallback)
    engine.close()


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


def test_teacher_broker_does_not_idle_timeout_inflight_request(tmp_path):
    child = tmp_path / "slow-mcp.py"

    child.write_text(
        """#!/usr/bin/env python3
import json
import sys
import time

for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")

    if method == "notifications/initialized":
        continue

    if method == "initialize":
        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "slow-test", "version": "1"},
        }

    elif method == "tools/list":
        # Più lungo dell'idle timeout del broker.
        time.sleep(2.0)
        result = {
            "tools": [{
                "name": "slow_tool",
                "description": "slow",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False,
                },
            }]
        }

    else:
        continue

    print(
        json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "result": result,
        }),
        flush=True,
    )
"""
    )
    child.chmod(0o700)

    socket_path = tmp_path / "slow.sock"

    broker = subprocess.Popen(
        [
            sys.executable,
            "scripts/ralf_teacher_mcp_broker.py",
            "--socket",
            str(socket_path),
            "--allow-uid",
            str(os.getuid()),
            "--command",
            str(child),
            "--idle-timeout",
            "1",
            "--max-clients",
            "1",
        ],
    )

    try:
        deadline = time.monotonic() + 3.0

        while (
            not socket_path.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)

        with MCPClientSession(
            UnixMCPTransport(str(socket_path)),
            timeout=5,
        ) as session:
            tools = session.list_tools()

        assert [tool.name for tool in tools] == ["slow_tool"]

    finally:
        broker.terminate()
        broker.wait(timeout=5)



def test_teacher_inference_unit_declares_grounded_cpu_fallback():
    unit = Path("deploy/systemd/ralf-teacher-inference.service").read_text()
    assert "RALF_TEACHER_CPU_FALLBACK=1" in unit
    assert "RALF_TEACHER_CPU_FALLBACK_MODEL=gemma3:4b" in unit
    assert "RALF_TEACHER_CPU_FALLBACK_URL=http://127.0.0.1:11434" in unit
    assert "RALF_TEACHER_CPU_FAST_LANE=1" in unit
