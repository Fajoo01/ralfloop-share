from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from ralfloop_agent.providers.gpu_arbiter import InferenceGpuArbiter
from ralfloop_agent.providers.gpu_engine_scheduler import EngineProvenance, GpuEngineTransitionError, TransactionalGpuScheduler


class Chat:
    class Config:
        gpu_lock_path = Path("unused")
    config = Config()

    def __init__(self, *, enough=True):
        self.enough = enough
        self.running = False
        self.events = []

    def resource_gate_status(self, **kwargs):
        self.events.append("gate")
        return {"gpu_free_mib": 7000 if self.enough else 3000, "required_gpu_memory_mib": 5881}

    def ensure_available(self, *, preacquired_gpu_fd=None):
        assert preacquired_gpu_fd is not None
        self.events.append("start")
        self.running = True
        return {"server_started": True}

    def status(self):
        return {"managed": self.running}

    def stop(self):
        self.events.append("stop")
        self.running = False


class External:
    def __init__(self, active=True):
        self.active = active
        self.events = []
        self.provenance = EngineProvenance("agentcpm", 19093, "AgentCPM-Explore", Path("/model.gguf"), "process", pid=77)

    def discover(self):
        return self.provenance if self.active else None

    def stop(self, provenance):
        self.events.append("stop")
        self.active = False

    def start(self, provenance):
        self.events.append("start")
        self.active = True


def test_qwen_transaction_restores_initial_agentcpm_on_error(tmp_path):
    chat, external = Chat(), External()
    scheduler = TransactionalGpuScheduler(chat=chat, external=external, arbiter=InferenceGpuArbiter(tmp_path / "gpu.lock"))
    with pytest.raises(RuntimeError, match="inference failed"):
        with scheduler.engine_session("qwen_chat", task_id="magnolia") as state:
            assert state["initial_engine"] == "agentcpm"
            assert state["vram_after_release_mib"] == 7000
            raise RuntimeError("inference failed")
    assert chat.events == ["gate", "start", "stop"]
    assert external.events == ["stop", "start"]
    assert external.active is True
    assert not (tmp_path / "gpu.lock").exists()


def test_qwen_transaction_preserves_initially_stopped_agentcpm(tmp_path):
    external = External(active=False)
    scheduler = TransactionalGpuScheduler(chat=Chat(), external=external, arbiter=InferenceGpuArbiter(tmp_path / "gpu.lock"))
    with scheduler.engine_session("qwen_chat"):
        pass
    assert external.events == []


def test_healthy_coexisting_qwen_preserves_agentcpm(tmp_path):
    class HealthyChat(Chat):
        def health(self):
            return True

        def ensure_available(self, *, preacquired_gpu_fd=None):
            assert preacquired_gpu_fd is not None
            self.events.append("reuse")
            return {"server_started": False, "server_reused": True}

    chat, external = HealthyChat(), External()
    scheduler = TransactionalGpuScheduler(
        chat=chat,
        external=external,
        arbiter=InferenceGpuArbiter(tmp_path / "gpu.lock"),
    )

    with scheduler.engine_session("qwen_chat") as state:
        assert state["initial_engine"] == "qwen_chat_coexisting"
        assert state["external_preserved"] is True

    assert chat.events == ["reuse"]
    assert external.events == []
    assert external.active is True


def test_vram_is_reobserved_after_stop_and_fails_closed(tmp_path):
    chat, external = Chat(enough=False), External()
    scheduler = TransactionalGpuScheduler(chat=chat, external=external, arbiter=InferenceGpuArbiter(tmp_path / "gpu.lock"))
    with pytest.raises(GpuEngineTransitionError, match="insufficient_gpu_memory_after_release"):
        with scheduler.engine_session("qwen_chat"):
            pass
    assert chat.events == ["gate"]
    assert external.events == ["stop", "start"]


def test_agentcpm_stop_failure_prevents_qwen_start_and_releases_lease(tmp_path):
    class StopFails(External):
        def stop(self, provenance):
            self.events.append("stop")
            raise RuntimeError("stop failed")
    chat, external = Chat(), StopFails()
    scheduler = TransactionalGpuScheduler(chat=chat, external=external, arbiter=InferenceGpuArbiter(tmp_path / "gpu.lock"))
    with pytest.raises(RuntimeError, match="stop failed"):
        with scheduler.engine_session("qwen_chat"):
            pass
    assert "start" not in chat.events
    assert not (tmp_path / "gpu.lock").exists()


def test_restore_failure_is_surfaced(tmp_path):
    class RestoreFails(External):
        def start(self, provenance):
            self.events.append("start")
            raise RuntimeError("restore failed")
    scheduler = TransactionalGpuScheduler(chat=Chat(), external=RestoreFails(), arbiter=InferenceGpuArbiter(tmp_path / "gpu.lock"))
    with pytest.raises(GpuEngineTransitionError, match="agentcpm_restore_failed") as raised:
        with scheduler.engine_session("qwen_chat"):
            pass
    assert isinstance(raised.value.restore_error, RuntimeError)


def test_primary_error_is_not_hidden_when_restore_also_fails(tmp_path):
    class RestoreFails(External):
        def start(self, provenance):
            raise RuntimeError("restore failed")
    scheduler = TransactionalGpuScheduler(chat=Chat(), external=RestoreFails(), arbiter=InferenceGpuArbiter(tmp_path / "gpu.lock"))
    with pytest.raises(ValueError, match="primary") as raised:
        with scheduler.engine_session("qwen_chat"):
            raise ValueError("primary")
    assert any("restore also failed" in note for note in raised.value.__notes__)


def test_busy_or_concurrent_request_causes_no_engine_transition(tmp_path):
    arbiter = InferenceGpuArbiter(tmp_path / "gpu.lock")
    fd = arbiter.acquire_fd(provider="first", mode="test", task_id="one")
    external = External()
    scheduler = TransactionalGpuScheduler(chat=Chat(), external=external, arbiter=arbiter)
    try:
        with pytest.raises(GpuEngineTransitionError, match="gpu_scheduler_busy"):
            with scheduler.engine_session("qwen_chat"):
                pass
        assert external.events == []
    finally:
        arbiter.release_fd(fd)


def test_scheduler_owns_preacquired_fd_and_releases_it_once(tmp_path):
    class CountingArbiter(InferenceGpuArbiter):
        releases = 0
        def release_fd(self, fd):
            self.releases += 1
            super().release_fd(fd)
    arbiter = CountingArbiter(tmp_path / "gpu.lock")
    chat = Chat()
    scheduler = TransactionalGpuScheduler(chat=chat, external=External(active=False), arbiter=arbiter)
    with scheduler.engine_session("qwen_chat"):
        assert (tmp_path / "gpu.lock").exists()
    assert arbiter.releases == 1
    assert not (tmp_path / "gpu.lock").exists()



def test_brokered_qwen_lock_is_owned_by_broker(tmp_path):
    from types import SimpleNamespace

    lock = tmp_path / "gpu.lock"
    broker_acquired = []

    class ExternalNone:
        def discover(self):
            return None

        def stop(self, provenance):
            raise AssertionError("unexpected stop")

        def start(self, provenance):
            raise AssertionError("unexpected start")

    class BrokeredChat:
        lifecycle_client = object()

        def __init__(self):
            self.config = SimpleNamespace(gpu_lock_path=lock)

        def health(self):
            return False

        def resource_gate_status(self, *, llama_cpp_running):
            return {
                "gpu_free_mib": 8192,
                "required_gpu_memory_mib": 1024,
            }

        def ensure_available(self, preacquired_gpu_fd=None):
            assert preacquired_gpu_fd is None

            arbiter = InferenceGpuArbiter(lock)

            fd = arbiter.acquire_fd(
                provider="llama_cpp",
                mode="broker-test",
                task_id="broker-test",
            )

            broker_acquired.append(True)
            arbiter.release_fd(fd)

            return {"server_started": False}

    scheduler = TransactionalGpuScheduler(
        chat=BrokeredChat(),
        external=ExternalNone(),
        arbiter=InferenceGpuArbiter(lock),
    )

    with scheduler.engine_session(
        "qwen_chat",
        task_id="broker-test",
    ) as state:
        assert state["gpu_lock_owner"] == (
            "llama_lifecycle_broker"
        )

    assert broker_acquired == [True]


def test_local_qwen_receives_preacquired_gpu_fd(tmp_path):
    from types import SimpleNamespace

    lock = tmp_path / "gpu.lock"
    received = []

    class ExternalNone:
        def discover(self):
            return None

        def stop(self, provenance):
            raise AssertionError("unexpected stop")

        def start(self, provenance):
            raise AssertionError("unexpected start")

    class LocalChat:
        lifecycle_client = None

        def __init__(self):
            self.config = SimpleNamespace(gpu_lock_path=lock)

        def health(self):
            return False

        def resource_gate_status(self, *, llama_cpp_running):
            return {
                "gpu_free_mib": 8192,
                "required_gpu_memory_mib": 1024,
            }

        def ensure_available(self, preacquired_gpu_fd=None):
            assert isinstance(preacquired_gpu_fd, int)
            assert preacquired_gpu_fd >= 0

            received.append(preacquired_gpu_fd)

            return {"server_started": False}

    scheduler = TransactionalGpuScheduler(
        chat=LocalChat(),
        external=ExternalNone(),
        arbiter=InferenceGpuArbiter(lock),
    )

    with scheduler.engine_session(
        "qwen_chat",
        task_id="local-test",
    ) as state:
        assert state["gpu_lock_owner"] == "scheduler_caller"

    assert len(received) == 1
