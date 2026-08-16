from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess

import pytest
from fastapi.testclient import TestClient

from ralfloop_agent.providers.agent_gpu_handoff import AgentGpuCoordinator
from ralfloop_agent.providers.chat import (
    ChatResult,
    ChatProviderUnavailable,
    FallbackChatProvider,
)
from ralfloop_agent.providers.gpu_arbiter import InferenceGpuArbiter
from ralfloop_agent.providers.llama_cpp import build_llama_cpp_chat_provider
from ralfloop_agent.providers.llama_cpp_server import (
    LlamaCppProcessState,
    LlamaCppServerConfig,
    LlamaCppServerManager,
    LlamaCppServerUnavailable,
    ProcessIdentity,
    _local_child,
    _remember_local_child,
    _reap_children_at_exit,
)


class FakeProcess:
    def __init__(
        self,
        pid: int,
        *,
        wait_actions: list[object] | None = None,
        alive: bool = True,
        returncode: int = 0,
        terminate_lookup_error: bool = False,
    ) -> None:
        self.pid = pid
        self.alive = alive
        self.returncode = returncode
        self.wait_actions = list(wait_actions or [returncode])
        self.terminate_lookup_error = terminate_lookup_error
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_timeouts: list[float] = []
        self.reference_seen_during_wait = False

    def poll(self):
        return None if self.alive else self.returncode

    def terminate(self):
        self.terminate_calls += 1
        if self.terminate_lookup_error:
            raise ProcessLookupError

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout):
        self.wait_timeouts.append(timeout)
        self.reference_seen_during_wait = self.reference_seen_during_wait or _local_child(self.pid) is self
        action = self.wait_actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        self.returncode = int(action)
        self.alive = False
        return self.returncode


def _config(tmp_path: Path, *, port: int = 19191, startup_timeout_sec: float = 1.0) -> LlamaCppServerConfig:
    tmp_path.mkdir(parents=True, exist_ok=True)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    server = tmp_path / "llama-server"
    server.write_text("fixture", encoding="utf-8")
    server.chmod(0o700)
    return LlamaCppServerConfig(
        base_url=f"http://127.0.0.1:{port}",
        model_path=model,
        model_hash=hashlib.sha256(model.read_bytes()).hexdigest(),
        server_bin=server,
        state_dir=tmp_path / "state",
        startup_timeout_sec=startup_timeout_sec,
    )


def _identity(config: LlamaCppServerConfig, pid: int, *, ticks: int = 7) -> ProcessIdentity:
    return ProcessIdentity(
        pid=pid,
        uid=os.getuid(),
        start_ticks=ticks,
        executable=str(config.server_bin),
        argv=(
            str(config.server_bin),
            "--alias",
            config.model,
            "--host",
            "127.0.0.1",
            "--port",
            str(config.port),
            "--model",
            str(config.model_path),
        ),
    )


def _pid_record(
    config: LlamaCppServerConfig,
    pid: int,
    ticks: int,
    *,
    owner_pid: int | None,
) -> dict:
    return {
        "pid": pid,
        "owner_pid": owner_pid,
        "owner_uid": os.getuid(),
        "start_ticks": ticks,
        "process_start_ticks": ticks,
        "provider": "llama_cpp",
        "mode": "chat",
        "server_bin": str(config.server_bin.resolve()),
        "model_path": str(config.model_path),
        "model_hash": config.model_hash,
        "port": config.port,
    }


def _owned_manager(tmp_path: Path, process: FakeProcess) -> LlamaCppServerManager:
    config = _config(tmp_path)
    identity = _identity(config, process.pid)
    manager = LlamaCppServerManager(
        config,
        identity_reader=lambda pid: identity if pid == process.pid and process.alive else None,
    )
    config.state_dir.mkdir()
    config.pid_path.write_text(
        json.dumps(
            _pid_record(
                config, process.pid, identity.start_ticks, owner_pid=os.getpid()
            )
        ),
        encoding="utf-8",
    )
    config.pid_path.chmod(0o600)
    _remember_local_child(process, lifecycle_key=manager.lifecycle_key)
    return manager


def test_start_stop_wait_and_reference_clears_only_after_wait(tmp_path: Path) -> None:
    process = FakeProcess(41001)
    manager = _owned_manager(tmp_path, process)
    result = manager.stop(timeout_sec=2.0)
    assert result["returncode"] == 0
    assert process.terminate_calls == 1
    assert process.wait_timeouts == [2.0]
    assert process.reference_seen_during_wait is True
    assert _local_child(process.pid) is None
    assert manager.status()["lifecycle_state"] == LlamaCppProcessState.STOPPED.value


def test_terminate_timeout_kill_then_mandatory_wait(tmp_path: Path) -> None:
    process = FakeProcess(
        41002,
        wait_actions=[subprocess.TimeoutExpired("llama-server", 1.0), -9],
    )
    manager = _owned_manager(tmp_path, process)
    result = manager.stop(timeout_sec=1.0)
    assert result["returncode"] == -9
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_timeouts == [1.0, 5.0]
    assert _local_child(process.pid) is None


def test_process_lookup_and_already_exited_are_still_waited(tmp_path: Path) -> None:
    lookup = FakeProcess(41003, terminate_lookup_error=True)
    manager = _owned_manager(tmp_path, lookup)
    manager.stop()
    assert lookup.wait_timeouts == [15.0]

    exited = FakeProcess(41004, alive=False, returncode=7, wait_actions=[7])
    manager = _owned_manager(tmp_path / "exited", exited)
    result = manager.stop()
    assert result["returncode"] == 7
    assert exited.terminate_calls == 0
    assert exited.wait_timeouts == [15.0]


def test_stop_is_idempotent_and_double_stop_does_not_signal(tmp_path: Path) -> None:
    process = FakeProcess(41005)
    manager = _owned_manager(tmp_path, process)
    assert manager.stop()["changed"] is True
    assert manager.stop()["changed"] is False
    assert process.terminate_calls == 1
    assert process.wait_timeouts == [15.0]


def test_lost_popen_reference_uses_owner_waitpid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.state_dir.mkdir()
    pid = 41006
    identity = _identity(config, pid)
    config.pid_path.write_text(
        json.dumps(
            _pid_record(config, pid, identity.start_ticks, owner_pid=os.getpid())
        ),
        encoding="utf-8",
    )
    config.pid_path.chmod(0o600)
    waits = []
    manager = LlamaCppServerManager(
        config,
        identity_reader=lambda requested: None,
        waitpid_fn=lambda requested, options: waits.append((requested, options)) or (pid, 0),
    )
    result = manager.stop()
    assert result["changed"] is True
    assert result["returncode"] == 0
    assert waits == [(pid, os.WNOHANG)]


def test_startup_exit_and_health_timeout_reap_and_fail(tmp_path: Path) -> None:
    for suffix, process, timeout in (
        ("exit", FakeProcess(41007, alive=False, returncode=3, wait_actions=[3]), 1.0),
        ("health", FakeProcess(41008), 0.01),
    ):
        config = _config(tmp_path / suffix, port=19200 + process.pid % 100, startup_timeout_sec=timeout)
        identity = _identity(config, process.pid)
        ticks = iter(range(1000))
        manager = LlamaCppServerManager(
            config,
            popen_factory=lambda *args, selected=process, **kwargs: selected,
            identity_reader=lambda pid, selected=identity: selected,
            monotonic=lambda: float(next(ticks)),
            sleep_fn=lambda seconds: None,
        )
        manager.health = lambda: False
        manager.ollama_gpu_models = lambda: []
        manager.resource_gate_status = lambda **kwargs: {"gate_reason": "gpu_available"}
        manager._port_in_use = lambda: False
        with pytest.raises(LlamaCppServerUnavailable):
            manager.start()
        assert process.wait_timeouts
        assert _local_child(process.pid) is None
        assert manager.status()["lifecycle_state"] == LlamaCppProcessState.FAILED.value


def test_new_start_after_stop_replaces_pid_and_health(tmp_path: Path) -> None:
    config = _config(tmp_path, port=19221)
    created: list[FakeProcess] = []
    pids = iter((41009, 41010))

    def popen(*args, **kwargs):
        process = FakeProcess(next(pids))
        created.append(process)
        return process

    def identity(pid):
        return _identity(config, pid) if any(item.pid == pid and item.alive for item in created) else None

    manager = LlamaCppServerManager(config, popen_factory=popen, identity_reader=identity)
    manager.health = lambda: any(item.alive for item in created)
    manager.ollama_gpu_models = lambda: []
    manager._port_in_use = lambda: False
    manager.resource_gate_status = lambda **kwargs: {"gate_reason": "gpu_available"}
    first = manager.ensure_available()
    assert manager.status()["lifecycle_state"] == LlamaCppProcessState.RUNNING.value
    manager.stop()
    assert manager.status()["lifecycle_state"] == LlamaCppProcessState.STOPPED.value
    second = manager.ensure_available()
    assert manager.status()["lifecycle_state"] == LlamaCppProcessState.RUNNING.value
    assert first["pid"] == 41009
    assert second["pid"] == 41010
    assert first["pid"] != second["pid"]
    assert created[0].wait_timeouts == [15.0]
    manager.stop()
    assert created[1].wait_timeouts == [15.0]


def test_agent_session_cancellation_reaps_before_cleanup(tmp_path: Path) -> None:
    process = FakeProcess(41011)
    manager = _owned_manager(tmp_path, process)
    manager.ollama_gpu_models = lambda: []
    coordinator = AgentGpuCoordinator(
        server_manager=manager,
        arbiter=InferenceGpuArbiter(manager.config.gpu_lock_path),
        port_in_use=lambda host, port: False,
    )
    with pytest.raises(asyncio.CancelledError):
        with coordinator.agent_session(models=(), task_id="cancelled"):
            raise asyncio.CancelledError
    assert process.wait_timeouts == [15.0]
    assert _local_child(process.pid) is None
    assert not manager.config.gpu_lock_path.exists()


def test_backend_exit_reaper_waits_for_owned_child(tmp_path: Path) -> None:
    process = FakeProcess(41012)
    manager = _owned_manager(tmp_path, process)
    _reap_children_at_exit()
    assert process.terminate_calls == 1
    assert process.wait_timeouts == [5.0]
    assert _local_child(process.pid) is None
    assert manager.status()["lifecycle_state"] == LlamaCppProcessState.STOPPED.value


def test_detached_cli_child_survives_exit_and_is_cross_process_stoppable(tmp_path: Path) -> None:
    config = _config(tmp_path, port=19222)
    process = FakeProcess(41013)
    identity = _identity(config, process.pid)
    popen_kwargs = {}

    def popen(*args, **kwargs):
        popen_kwargs.update(kwargs)
        return process

    manager = LlamaCppServerManager(
        config,
        popen_factory=popen,
        identity_reader=lambda pid: identity if pid == process.pid and process.alive else None,
    )
    manager.health = lambda: process.alive and _local_child(process.pid) is process
    manager._port_in_use = lambda: False
    manager.resource_gate_status = lambda **kwargs: {"gate_reason": "gpu_available"}

    result = manager.start(detach=True)
    record = json.loads(config.pid_path.read_text(encoding="utf-8"))
    _reap_children_at_exit()

    assert result["detached"] is True
    assert popen_kwargs["env"]["GGML_CUDA_ENABLE_UNIFIED_MEMORY"] == "1"
    assert record["owner_pid"] is None
    assert process.terminate_calls == 0
    assert process.alive is True
    assert _local_child(process.pid) is None

    signals = []

    def kill(pid, sig):
        signals.append((pid, sig))
        process.alive = False

    stopper = LlamaCppServerManager(
        config,
        identity_reader=lambda pid: identity if pid == process.pid and process.alive else None,
        kill_fn=kill,
        sleep_fn=lambda seconds: None,
    )
    stopped = stopper.stop()

    assert stopped["changed"] is True
    assert signals == [(process.pid, signal.SIGTERM)]
    assert not config.pid_path.exists()


class _Primary:
    name = "llama_cpp"
    default_model = "qwen2.5:7b"

    def __init__(self, *, fail: bool) -> None:
        self.fail = fail

    def chat(self, messages, *, model=None):
        if self.fail:
            raise ChatProviderUnavailable("llama_cpp_server_unavailable")
        return ChatResult("primary", self.default_model, self.name, {})


class _Fallback:
    name = "ollama"
    default_model = "qwen2.5:7b"

    def chat(self, messages, *, model=None):
        return ChatResult("fallback", self.default_model, self.name, {})


def test_fallback_configured_is_distinct_from_fallback_used() -> None:
    unused = FallbackChatProvider(
        name="llama_cpp",
        primary=_Primary(fail=False),
        fallback=_Fallback(),
        configured_fallback="ollama",
    ).chat([{"role": "user", "content": "q"}])
    assert unused.metadata["configured_fallback"] == "ollama"
    assert unused.metadata["fallback_used"] is False

    used = FallbackChatProvider(
        name="llama_cpp",
        primary=_Primary(fail=True),
        fallback=_Fallback(),
        configured_fallback="ollama",
    ).chat([{"role": "user", "content": "q"}])
    assert used.metadata["configured_fallback"] == "ollama"
    assert used.metadata["fallback_used"] is True
    assert used.metadata["fallback_provider"] == "ollama"
    assert used.metadata["fallback_reason"] == "llama_cpp_server_unavailable"


def test_fallback_none_never_wraps_or_calls_ollama(tmp_path: Path) -> None:
    config = _config(tmp_path)
    manager = type(
        "Manager",
        (),
        {"ensure_available": lambda self: (_ for _ in ()).throw(LlamaCppServerUnavailable())},
    )()
    provider = build_llama_cpp_chat_provider(config=config, manager=manager, fallback=_Fallback())
    with pytest.raises(ChatProviderUnavailable):
        provider.chat([{"role": "user", "content": "q"}])


def test_backend_shutdown_invokes_managed_child_reaper(monkeypatch) -> None:
    from openshell_backend.app import app
    from ralfloop_agent.providers import llama_cpp_server

    calls: list[bool] = []
    monkeypatch.setattr(llama_cpp_server, "_reap_children_at_exit", lambda: calls.append(True))
    with TestClient(app):
        pass
    assert calls == [True]
