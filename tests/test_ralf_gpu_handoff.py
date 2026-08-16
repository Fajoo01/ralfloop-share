from __future__ import annotations

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path

import pytest

from openshell_backend import app as backend_app
from openshell_backend import chat_api
from ralfloop_agent.cli import terminal_chat as cli
from ralfloop_agent.providers import agent_gpu_handoff as handoff_module
from ralfloop_agent.providers.agent_gpu_handoff import (
    AgentGpuCoordinator,
    AgentGpuHandoffError,
)
from ralfloop_agent.providers.chat import ChatProviderUnavailable
from ralfloop_agent.providers.gpu_arbiter import GpuArbiterBusy, InferenceGpuArbiter
from ralfloop_agent.providers.llama_cpp import _fallback_allowed
from ralfloop_agent.providers.llama_cpp_server import LlamaCppServerConfig
from ralfloop_agent.providers.llama_cpp_server import (
    LlamaCppServerManager,
    ProcessIdentity,
    _local_child,
    _remember_local_child,
)


class FakeResponse:
    def raise_for_status(self):
        return None

    def close(self):
        return None


class FakeSession:
    def __init__(self):
        self.models = []

    def post(self, url, **kwargs):
        self.models.append(kwargs["json"]["model"])
        return FakeResponse()


class FakeServerManager:
    def __init__(self, tmp_path: Path, *, managed=False, healthy=False, models=()):
        self.config = LlamaCppServerConfig(
            state_dir=tmp_path / "state",
            model_path=tmp_path / "model.gguf",
            server_bin=tmp_path / "llama-server",
            model_hash="0" * 64,
        )
        self.managed = managed
        self.healthy = healthy
        self.port_busy = managed or healthy
        self.model_states = [set(item) for item in models]
        self.stop_calls = 0
        self.ensure_calls = 0

    def status(self):
        return {"managed": self.managed, "healthy": self.healthy}

    def stop(self):
        self.stop_calls += 1
        self.managed = False
        self.healthy = False
        self.port_busy = False
        return {"changed": True}

    def ensure_available(self):
        self.ensure_calls += 1
        self.managed = True
        self.healthy = True
        self.port_busy = True
        return {"server_started": True, "server_reused": False}

    def ollama_gpu_models(self):
        if self.model_states:
            return sorted(self.model_states.pop(0))
        return []


def _coordinator(tmp_path: Path, manager: FakeServerManager, **kwargs) -> AgentGpuCoordinator:
    return AgentGpuCoordinator(
        server_manager=manager,
        arbiter=InferenceGpuArbiter(manager.config.gpu_lock_path),
        port_in_use=lambda host, port: manager.port_busy,
        **kwargs,
    )


def test_gpu_arbiter_lock_concurrency_metadata_and_release(tmp_path):
    path = tmp_path / "inference-gpu.lock"
    first = InferenceGpuArbiter(path)
    second = InferenceGpuArbiter(path)
    fd = first.acquire_fd(provider="llama_cpp", mode="chat", model_hash="a" * 64)
    status = second.status()
    assert status.held is True
    assert status.metadata["provider"] == "llama_cpp"
    assert "prompt" not in status.metadata
    with pytest.raises(GpuArbiterBusy):
        second.acquire_fd(provider="ollama", mode="agent")
    first.release_fd(fd)
    assert second.status().held is False


def test_gpu_arbiter_stale_lock_is_cleaned(tmp_path):
    path = tmp_path / "inference-gpu.lock"
    path.write_text(json.dumps({"pid": 999999, "mode": "chat"}), encoding="utf-8")
    status = InferenceGpuArbiter(path).status(clean_stale=True)
    assert status.held is False
    assert status.stale is True
    assert not path.exists()


def test_agent_switch_stops_only_managed_server_and_frees_port(tmp_path):
    manager = FakeServerManager(tmp_path, managed=True, healthy=True)
    result = _coordinator(tmp_path, manager).switch_agent()
    assert manager.stop_calls == 1
    assert result["llama_cpp_running"] is False
    assert result["port_19091_free"] is True


def test_manager_reaps_child_started_in_same_backend_process(tmp_path):
    server = tmp_path / "llama-server"
    server.write_text("x", encoding="utf-8")
    config = LlamaCppServerConfig(
        state_dir=tmp_path / "state",
        model_path=tmp_path / "model.gguf",
        server_bin=server,
        model_hash="0" * 64,
    )
    config.state_dir.mkdir()
    pid = 424242
    identity = ProcessIdentity(
        pid=pid,
        uid=os.getuid(),
        start_ticks=7,
        executable=str(server),
        argv=(
            str(server), "--alias", config.model, "--host", "127.0.0.1",
            "--port", "19091", "--model", str(config.model_path),
        ),
    )
    config.pid_path.write_text(
        json.dumps(
            {
                "pid": pid,
                "owner_pid": os.getpid(),
                "owner_uid": os.getuid(),
                "start_ticks": 7,
                "process_start_ticks": 7,
                "provider": "llama_cpp",
                "mode": "chat",
                "server_bin": str(config.server_bin.resolve()),
                "model_path": str(config.model_path),
                "model_hash": config.model_hash,
                "port": config.port,
            }
        ),
        encoding="utf-8",
    )
    config.pid_path.chmod(0o600)

    class Child:
        def __init__(self):
            self.pid = pid
            self.waits = []

        def wait(self, timeout):
            self.waits.append(timeout)
            return 0

    child = Child()
    _remember_local_child(child)
    killed = []
    manager = LlamaCppServerManager(
        config,
        identity_reader=lambda requested: identity if requested == pid else None,
        kill_fn=lambda requested, sig: killed.append((requested, sig)),
    )
    result = manager.stop()
    assert result["changed"] is True
    assert child.waits == [15.0]
    assert _local_child(pid) is None
    assert killed and killed[0][0] == pid


def test_agent_switch_does_not_stop_unmanaged_server(tmp_path):
    manager = FakeServerManager(tmp_path, managed=False, healthy=True)
    with pytest.raises(AgentGpuHandoffError, match="unmanaged"):
        _coordinator(tmp_path, manager).switch_agent()
    assert manager.stop_calls == 0


def test_agent_cleanup_finally_unloads_only_task_models_and_preserves_foreign(tmp_path):
    manager = FakeServerManager(
        tmp_path,
        models=(
            {"foreign:1"},
            {"foreign:1", "qwen2.5:7b"},
            {"foreign:1"},
        ),
    )
    session = FakeSession()
    coordinator = _coordinator(tmp_path, manager, session=session)
    with pytest.raises(RuntimeError, match="cancelled"):
        with coordinator.agent_session(models=("qwen2.5:7b",), task_id="safe-task"):
            raise RuntimeError("cancelled")
    assert session.models == ["qwen2.5:7b"]
    assert not manager.config.gpu_lock_path.exists()


def test_agent_cleanup_preserves_model_loaded_before_task(tmp_path):
    manager = FakeServerManager(
        tmp_path,
        models=(
            {"qwen2.5:7b", "foreign:1"},
            {"qwen2.5:7b", "foreign:1"},
        ),
    )
    session = FakeSession()
    with _coordinator(tmp_path, manager, session=session).agent_session(models=("qwen2.5:7b",)):
        pass
    assert session.models == []


def test_chat_switch_restarts_lazily_after_agent(tmp_path):
    manager = FakeServerManager(tmp_path)
    result = _coordinator(tmp_path, manager).switch_chat()
    assert manager.ensure_calls == 1
    assert result["mode"] == "chat"
    assert result["llama_cpp_running"] is True


def test_agent_lock_blocks_chat_without_fallback(tmp_path):
    manager = FakeServerManager(tmp_path)
    coordinator = _coordinator(tmp_path, manager)
    fd = coordinator.arbiter.acquire_fd(provider="ollama", mode="agent")
    try:
        with pytest.raises(AgentGpuHandoffError, match="agent_task_active"):
            coordinator.switch_chat()
        assert _fallback_allowed(ChatProviderUnavailable("llama_cpp_gpu_lock_busy")) is False
    finally:
        coordinator.arbiter.release_fd(fd)


def test_backend_tasks_run_uses_backend_level_agent_session(monkeypatch):
    events = []

    class Coordinator:
        def __init__(self, **kwargs):
            pass

        @contextmanager
        def agent_session(self, *, models, task_id):
            events.append(("enter", tuple(models)))
            try:
                yield {}
            finally:
                events.append(("exit", tuple(models)))

    monkeypatch.setattr(handoff_module, "AgentGpuCoordinator", Coordinator)
    monkeypatch.setattr(backend_app, "_run_task_impl", lambda req: {"ok": True})
    request = backend_app.TaskRunRequest(user_goal="read-only")
    assert backend_app.run_task(request) == {"ok": True}
    assert events == [
        ("enter", ("qwen2.5:7b", "qwen2.5:7b", "qwen2.5:7b")),
        ("exit", ("qwen2.5:7b", "qwen2.5:7b", "qwen2.5:7b")),
    ]


def test_default_api_provider_is_llama_cpp(monkeypatch):
    class Provider:
        name = "llama_cpp"

    monkeypatch.delenv("RALF_CHAT_PROVIDER", raising=False)
    monkeypatch.setattr(chat_api, "build_experimental_provider", lambda name: Provider())
    chat_api.get_chat_provider.cache_clear()
    try:
        assert chat_api.get_chat_provider().name == "llama_cpp"
    finally:
        chat_api.get_chat_provider.cache_clear()


def test_cli_engine_switch_and_handoff_status(tmp_path):
    manager = FakeServerManager(tmp_path)

    class Coordinator:
        def switch_chat(self):
            return {"mode": "chat"}

        def switch_agent(self):
            return {"mode": "agent_ready"}

        def handoff_status(self):
            return {"gpu_lock": "free"}

    for argv, expected in (
        (["engine", "switch", "chat"], '"mode": "chat"'),
        (["engine", "switch", "agent"], '"mode": "agent_ready"'),
        (["engine", "handoff-status"], '"gpu_lock": "free"'),
    ):
        args = cli.build_parser().parse_args(argv)
        out = io.StringIO()
        action = args.engine_action.replace("-", "_")
        if action == "switch":
            action = f"switch_{args.mode}"
        assert cli.run_engine_action(action, manager=manager, coordinator=Coordinator(), out=out) == 0
        assert expected in out.getvalue()


def test_production_environment_dropin_and_rollback_are_bounded():
    root = Path(__file__).resolve().parents[1]
    lab = root / ".ralf_run" / "ralf_llama_cpp_final_deploy"
    env = (lab / "fast-chat.env").read_text(encoding="utf-8")
    dropin = (lab / "60-fast-chat-llama-cpp.conf").read_text(encoding="utf-8")
    rollback = (lab / "rollback.sh").read_text(encoding="utf-8")
    assert "RALF_CHAT_PROVIDER=llama_cpp" in env
    assert "RALF_LLAMA_CPP_AUTOSTART=1" in env
    assert "RALF_LLAMA_CPP_NGRAM=0" in env
    assert "EnvironmentFile=-/etc/ralfloop/fast-chat.env" in dropin
    assert "ralfloop-backend.service" in rollback
    for forbidden in ("pkill", "killall", "ollama.service", "auto-approve", "auto-execute"):
        assert forbidden not in rollback


def test_no_chat_fallback_to_tasks_run_or_automatic_approval():
    root = Path(__file__).resolve().parents[1]
    chat_sources = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "openshell_backend/chat_api.py",
            "ralfloop_agent/providers/chat.py",
            "ralfloop_agent/providers/llama_cpp.py",
        )
    )
    assert "/tasks/run" not in chat_sources
    assert "auto-approve" not in chat_sources
    assert "auto-execute" not in chat_sources


def test_magnolia_host_runner_loads_existing_safe_approval_configuration():
    root = Path(__file__).resolve().parents[1]
    source = (root / "tools/run_gpu_handoff_magnolia.py").read_text(encoding="utf-8")
    workflow = (root / "tools/run_magnolia_workflow.py").read_text(encoding="utf-8")
    assert 'load_env(Path("/etc/ralfloop/telegram-approval.env"))' in source
    assert "if not policy.enabled or policy.auto_execute" in workflow
    assert "approval gate must be enabled with auto-execute disabled" in workflow
    assert "magnolia_main(host_diagnostics=True)" in source
    for marker in (
        '{label}_DRAFT_BEGIN', '{label}_VALIDATION_REASON=',
        "FINAL_DRAFT_BEGIN", "FINAL_VALIDATION=",
        "APPROVAL_ID=", "TELEGRAM_DELIVERED=", "TELEGRAM_MESSAGE_ID=", "EMAIL_SENT=false",
        "REJECTED_DRAFT_BEGIN", "REJECTED_DRAFT_END",
    ):
        assert marker in workflow
