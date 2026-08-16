from __future__ import annotations

import io
import json
import os
from pathlib import Path
import signal

import pytest
import requests

from openshell_backend import chat_api
from ralfloop_agent.cli import terminal_chat as cli
from ralfloop_agent.providers.chat import (
    ChatChunk,
    ChatConnectTimeout,
    ChatInactivityTimeout,
    ChatInvalidResponse,
    ChatResult,
)
from ralfloop_agent.providers.llama_cpp import (
    LlamaCppChatProvider,
    build_llama_cpp_chat_provider,
)
from ralfloop_agent.providers.llama_cpp_server import (
    DEFAULT_MODEL_HASH,
    LlamaCppServerBusy,
    LlamaCppServerConfig,
    LlamaCppServerManager,
    LlamaCppServerOwnershipError,
    LlamaCppServerUnavailable,
    ProcessIdentity,
    verify_model_hash,
)


class FakeResponse:
    def __init__(self, *, status_code=200, payload=None, lines=()):
        self.status_code = status_code
        self.payload = payload
        self.lines = list(lines)
        self.closed = False

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(str(self.status_code))
            error.response = self
            raise error

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_lines(self, **kwargs):
        yield from self.lines

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, *, posts=(), gets=()):
        self.posts = list(posts)
        self.gets = list(gets)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        item = self.posts.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        item = self.gets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeManager:
    def __init__(self, result=None, error=None):
        self.result = result or {"server_started": False, "server_startup_ms": None}
        self.error = error

    def ensure_available(self):
        if self.error:
            raise self.error
        return self.result


class FakeFallback:
    name = "ollama"
    default_model = "qwen2.5:7b"

    def __init__(self):
        self.calls = 0

    def chat(self, messages, *, model=None):
        self.calls += 1
        return ChatResult("fallback", self.default_model, self.name)

    def stream_chat(self, messages, *, model=None):
        self.calls += 1
        yield ChatChunk(text="fallback", model=self.default_model)
        yield ChatChunk(done=True, model=self.default_model)


def _config(tmp_path: Path, **overrides) -> LlamaCppServerConfig:
    values = {
        "model_path": tmp_path / "model.gguf",
        "model_hash": "0" * 64,
        "server_bin": tmp_path / "llama-server",
        "state_dir": tmp_path / "state",
    }
    values.update(overrides)
    return LlamaCppServerConfig(**values)


def _pid_record(config: LlamaCppServerConfig, pid: int, ticks: int, *, uid: int | None = None) -> dict:
    return {
        "pid": pid,
        "owner_pid": None,
        "owner_uid": os.getuid() if uid is None else uid,
        "start_ticks": ticks,
        "process_start_ticks": ticks,
        "provider": "llama_cpp",
        "mode": "chat",
        "server_bin": str(config.server_bin.resolve()),
        "model_path": str(config.model_path),
        "model_hash": config.model_hash,
        "port": config.port,
    }


def _stream_response(*events: dict | str) -> FakeResponse:
    lines = []
    for event in events:
        data = event if isinstance(event, str) else json.dumps(event)
        lines.append(f"data: {data}")
    return FakeResponse(lines=lines)


def test_defaults_are_llama_cpp_with_autostart_enabled(monkeypatch):
    for name in ("RALF_CHAT_PROVIDER", "RALF_LLAMA_CPP_AUTOSTART"):
        monkeypatch.delenv(name, raising=False)
    assert cli.ChatConfig.from_env().provider == "llama_cpp"
    config = LlamaCppServerConfig.from_env()
    assert config.autostart is True
    assert config.base_url == "http://127.0.0.1:19091"
    assert config.gpu_layers == 24
    assert config.batch_size == 64
    assert config.ubatch_size == 64
    assert config.kv_offload is False
    assert config.op_offload is False
    assert config.unified_memory is True
    assert config.allow_healthy_reuse is False
    assert config.required_gpu_memory_mib == 3600
    assert config.slots == 1
    assert config.cache_prompt is True
    assert config.cache_ram_mib == 1024
    assert config.ngram is False
    assert config.model_path.name == f"sha256-{DEFAULT_MODEL_HASH}"
    assert config.model_hash == DEFAULT_MODEL_HASH


def test_config_requires_loopback_and_one_slot():
    with pytest.raises(ValueError, match="loopback"):
        LlamaCppServerConfig(base_url="http://0.0.0.0:19091")
    with pytest.raises(ValueError, match="loopback"):
        LlamaCppServerConfig(base_url="http://127.0.0.1:19091/prefix")
    with pytest.raises(ValueError, match="slots"):
        LlamaCppServerConfig(slots=2)


def test_model_hash_fixture(tmp_path):
    model = tmp_path / "fixture.gguf"
    model.write_bytes(b"GGUF-test")
    import hashlib

    expected = hashlib.sha256(model.read_bytes()).hexdigest()
    assert verify_model_hash(model, expected) == expected
    with pytest.raises(Exception, match="hash_mismatch"):
        verify_model_hash(model, "0" * 64)


def test_server_command_is_local_cached_single_slot_without_speculation(tmp_path):
    config = _config(tmp_path)
    command = LlamaCppServerManager(config).command()
    joined = " ".join(command)
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--parallel") + 1] == "1"
    assert command[command.index("--n-gpu-layers") + 1] == "24"
    assert command[command.index("--batch-size") + 1] == "64"
    assert command[command.index("--ubatch-size") + 1] == "64"
    assert command[command.index("--fit") + 1] == "off"
    assert "--no-kv-offload" in command
    assert "--no-op-offload" in command
    assert "--cache-prompt" in command
    assert "ngram" not in joined
    assert "speculative" not in joined
    assert "draft" not in joined


def test_health_check_closes_response(tmp_path):
    response = FakeResponse(status_code=200, payload={"status": "ok"})
    models = FakeResponse(status_code=200, payload={"data": [{"id": "qwen2.5:7b"}]})
    manager = LlamaCppServerManager(_config(tmp_path), session=FakeSession(gets=[response, models]))
    assert manager.health() is True
    assert response.closed is True
    assert models.closed is True


def test_health_rejects_wrong_model_alias(tmp_path):
    session = FakeSession(
        gets=[
            FakeResponse(status_code=200, payload={"status": "ok"}),
            FakeResponse(status_code=200, payload={"data": [{"id": "other"}]}),
        ]
    )
    assert LlamaCppServerManager(_config(tmp_path), session=session).health() is False


def test_ensure_available_can_reuse_health_verified_cross_user_server(tmp_path):
    config = _config(tmp_path, allow_healthy_reuse=True)
    manager = LlamaCppServerManager(config)
    manager.health = lambda: True
    manager._managed_running = lambda: False

    assert manager.ensure_available() == {
        "server_started": False,
        "server_reused": True,
        "server_startup_ms": None,
        "managed": False,
    }


def test_non_streaming_parses_usage_and_timings(tmp_path):
    response = FakeResponse(
        payload={
            "model": "qwen2.5:7b",
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "prompt_tokens_details": {"cached_tokens": 8},
            },
            "timings": {
                "prompt_ms": 4.5,
                "prompt_per_second": 100.0,
                "predicted_per_second": 22.4,
            },
        }
    )
    session = FakeSession(posts=[response])
    provider = LlamaCppChatProvider(config=_config(tmp_path), manager=FakeManager(), session=session)
    result = provider.chat([{"role": "user", "content": "q"}])
    assert result.text == "ok"
    assert result.metadata["prompt_tokens"] == 12
    assert result.metadata["generated_tokens"] == 3
    assert result.metadata["cache_hit_tokens"] == 8
    assert result.metadata["prompt_tokens_per_second"] == 100.0
    assert result.metadata["gpu_layers"] == 24
    sent = session.calls[0][2]["json"]
    assert sent["cache_prompt"] is True
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["stream"] is False


def test_streaming_is_real_and_final_usage_is_retained(tmp_path):
    response = _stream_response(
        {"model": "qwen2.5:7b", "choices": [{"delta": {"content": "A"}}]},
        {
            "model": "qwen2.5:7b",
            "choices": [],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
            "timings": {"predicted_per_second": 20.0},
        },
        "[DONE]",
    )
    session = FakeSession(posts=[response])
    provider = LlamaCppChatProvider(config=_config(tmp_path), manager=FakeManager(), session=session)
    chunks = list(provider.stream_chat([{"role": "user", "content": "q"}]))
    assert [item.text for item in chunks if item.text] == ["A"]
    assert chunks[-1].done is True
    assert chunks[-1].metadata["generated_tokens"] == 1
    assert response.closed is True


def test_chat_api_keeps_ndjson_timing_metadata():
    class Provider:
        name = "llama_cpp"
        default_model = "qwen2.5:7b"

        def stream_chat(self, messages, *, model=None):
            yield ChatChunk(text="ok", model=self.default_model)
            yield ChatChunk(
                done=True,
                model=self.default_model,
                metadata={"ttft_ms": 100.0, "generated_tokens": 1},
            )

    events = [
        json.loads(line)
        for line in chat_api._stream_events(
            chat_api.ChatRequest(message="q"),
            Provider(),
            session_id="s",
        )
    ]
    assert events[-1]["type"] == "done"
    assert events[-1]["metadata"]["ttft_ms"] == 100.0


def test_timings_output_keeps_counts_without_secret_redaction():
    text = cli._timings_text(
        {
            "provider": "llama_cpp",
            "metadata": {
                "prompt_tokens": 12,
                "generated_tokens": 3,
                "cache_hit_tokens": 8,
                "prompt_tokens_per_second": 100.0,
                "decode_tokens_per_second": 22.4,
            },
        }
    )
    assert '"prompt_count": 12' in text
    assert '"generated_count": 3' in text
    assert '"prompt_rate": 100.0' in text
    assert '"decode_rate": 22.4' in text
    assert "REDACTED" not in text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (requests.ConnectTimeout("connect"), ChatConnectTimeout),
        (requests.ReadTimeout("idle"), ChatInactivityTimeout),
    ],
)
def test_connection_and_inactivity_timeouts_are_distinct(tmp_path, error, expected):
    provider = LlamaCppChatProvider(
        config=_config(tmp_path),
        manager=FakeManager(),
        session=FakeSession(posts=[error]),
    )
    with pytest.raises(expected):
        provider.chat([{"role": "user", "content": "q"}])


def test_cancellation_closes_stream_response(tmp_path):
    response = _stream_response(
        {"choices": [{"delta": {"content": "A"}}]},
        {"choices": [{"delta": {"content": "B"}}]},
        "[DONE]",
    )
    provider = LlamaCppChatProvider(
        config=_config(tmp_path),
        manager=FakeManager(),
        session=FakeSession(posts=[response]),
    )
    stream = provider.stream_chat([{"role": "user", "content": "q"}])
    assert next(stream).text == "A"
    stream.close()
    assert response.closed is True


def test_explicit_fallback_server_unavailable_is_reported(tmp_path):
    fallback = FakeFallback()
    provider = build_llama_cpp_chat_provider(
        fallback=fallback,
        config=_config(tmp_path, fallback="ollama"),
        manager=FakeManager(error=LlamaCppServerUnavailable()),
    )
    result = provider.chat([{"role": "user", "content": "q"}])
    assert result.text == "fallback"
    assert result.provider == "ollama"
    assert result.metadata["fallback_used"] is True
    assert fallback.calls == 1


def test_explicit_fallback_http_error_is_reported(tmp_path):
    fallback = FakeFallback()
    provider = build_llama_cpp_chat_provider(
        fallback=fallback,
        config=_config(tmp_path, fallback="ollama"),
        manager=FakeManager(),
        session=FakeSession(posts=[FakeResponse(status_code=400)]),
    )
    assert provider.chat([{"role": "user", "content": "q"}]).text == "fallback"
    assert fallback.calls == 1


def test_explicit_fallback_invalid_response_is_reported(tmp_path):
    fallback = FakeFallback()
    provider = build_llama_cpp_chat_provider(
        fallback=fallback,
        config=_config(tmp_path, fallback="ollama"),
        manager=FakeManager(),
        session=FakeSession(posts=[_stream_response("not-json")]),
    )
    chunks = list(provider.stream_chat([{"role": "user", "content": "q"}]))
    assert "".join(item.text for item in chunks) == "fallback"
    assert chunks[-1].metadata["fallback_used"] is True
    assert fallback.calls == 1


def test_no_fallback_after_first_stream_token(tmp_path):
    response = _stream_response(
        {"choices": [{"delta": {"content": "partial"}}]},
        "not-json",
    )
    fallback = FakeFallback()
    provider = build_llama_cpp_chat_provider(
        fallback=fallback,
        config=_config(tmp_path, fallback="ollama"),
        manager=FakeManager(),
        session=FakeSession(posts=[response]),
    )
    with pytest.raises(ChatInvalidResponse):
        list(provider.stream_chat([{"role": "user", "content": "q"}]))
    assert fallback.calls == 0


def test_inactivity_timeout_does_not_fallback(tmp_path):
    fallback = FakeFallback()
    provider = build_llama_cpp_chat_provider(
        fallback=fallback,
        config=_config(tmp_path, fallback="ollama"),
        manager=FakeManager(),
        session=FakeSession(posts=[requests.ReadTimeout("idle")]),
    )
    with pytest.raises(ChatInactivityTimeout):
        provider.chat([{"role": "user", "content": "q"}])
    assert fallback.calls == 0


def test_ollama_gpu_state_blocks_start_before_process_creation(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    import hashlib

    server = tmp_path / "llama-server"
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o700)
    config = _config(
        tmp_path,
        model_path=model,
        model_hash=hashlib.sha256(model.read_bytes()).hexdigest(),
        server_bin=server,
    )
    session = FakeSession(
        gets=[
            requests.ConnectionError("down"),
            requests.ConnectionError("down"),
            FakeResponse(payload={"models": [{"name": "qwen2.5:7b", "size_vram": 1024}]}),
        ]
    )
    calls = []
    manager = LlamaCppServerManager(
        config,
        session=session,
        popen_factory=lambda *a, **k: calls.append(a),
        gpu_observer=lambda: {"free_mib": 7425, "processes": []},
    )
    manager._port_in_use = lambda: False
    with pytest.raises(LlamaCppServerBusy, match="ollama_model_already_loaded"):
        manager.start()
    assert calls == []


def test_failed_start_cleans_pid_and_gpu_lock(tmp_path):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    import hashlib

    server = tmp_path / "llama-server"
    server.write_text("#!/bin/sh\n", encoding="utf-8")
    server.chmod(0o700)
    config = _config(
        tmp_path,
        model_path=model,
        model_hash=hashlib.sha256(model.read_bytes()).hexdigest(),
        server_bin=server,
    )
    session = FakeSession(
        gets=[
            requests.ConnectionError("down"),
            requests.ConnectionError("down"),
            FakeResponse(payload={"models": []}),
        ]
    )
    manager = LlamaCppServerManager(
        config,
        session=session,
        popen_factory=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")),
        gpu_observer=lambda: {"free_mib": 7425, "processes": []},
    )
    manager._port_in_use = lambda: False
    with pytest.raises(OSError, match="spawn failed"):
        manager.start()
    assert not config.pid_path.exists()
    assert not config.gpu_lock_path.exists()


def test_gpu_lock_is_advisory_and_exclusive(tmp_path):
    manager = LlamaCppServerManager(_config(tmp_path))
    manager._ensure_state_dirs()
    fd = manager._acquire_gpu_lock()
    try:
        with pytest.raises(LlamaCppServerBusy):
            LlamaCppServerManager(manager.config)._acquire_gpu_lock()
    finally:
        os.close(fd)


def test_stop_targets_only_validated_managed_pid(tmp_path):
    server = tmp_path / "llama-server"
    server.write_text("x", encoding="utf-8")
    config = _config(tmp_path, server_bin=server)
    config.state_dir.mkdir()
    pid = 12345
    alive = {"value": True}
    identity = ProcessIdentity(
        pid=pid,
        uid=os.getuid(),
        start_ticks=77,
        executable=str(server),
        argv=(
            str(server), "--alias", config.model, "--host", "127.0.0.1",
            "--port", "19091", "--model", str(config.model_path),
        ),
    )

    def reader(requested):
        return identity if requested == pid and alive["value"] else None

    killed = []

    def kill_fn(requested, sig):
        killed.append((requested, sig))
        alive["value"] = False

    config.pid_path.write_text(
        json.dumps(_pid_record(config, pid, 77)),
        encoding="utf-8",
    )
    config.pid_path.chmod(0o600)
    manager = LlamaCppServerManager(config, identity_reader=reader, kill_fn=kill_fn)
    result = manager.stop()
    assert result["changed"] is True
    assert killed == [(pid, signal.SIGTERM)]


def test_stop_refuses_foreign_or_mismatched_process(tmp_path):
    server = tmp_path / "llama-server"
    server.write_text("x", encoding="utf-8")
    config = _config(tmp_path, server_bin=server)
    config.state_dir.mkdir()
    pid = 222
    config.pid_path.write_text(
        json.dumps(_pid_record(config, pid, 1)),
        encoding="utf-8",
    )
    config.pid_path.chmod(0o600)
    foreign = ProcessIdentity(pid, os.getuid(), 1, str(server), (str(server), "--unrelated"))
    killed = []
    manager = LlamaCppServerManager(
        config,
        identity_reader=lambda requested: foreign,
        kill_fn=lambda *args: killed.append(args),
    )
    with pytest.raises(LlamaCppServerOwnershipError):
        manager.stop()
    assert killed == []


def test_engine_cli_status_start_stop_health_are_wired(tmp_path):
    class Manager:
        def __init__(self):
            self.calls = []

        def status(self):
            self.calls.append("status")
            return {"provider": "llama_cpp", "healthy": True}

        def start(self, *, dry_run=False, detach=False):
            self.calls.append(("start", dry_run, detach))
            return {"provider": "llama_cpp", "dry_run": dry_run}

        def stop(self):
            self.calls.append("stop")
            return {"provider": "llama_cpp", "status": "stopped"}

        def health(self):
            self.calls.append("health")
            return True

    manager = Manager()
    out = io.StringIO()
    args = cli.build_parser().parse_args(["engine", "start", "llama_cpp", "--dry-run"])
    assert cli.run_engine(args, manager=manager, out=out, err=io.StringIO()) == 0
    assert manager.calls == [("start", True, False)]
    assert '"provider": "llama_cpp"' in out.getvalue()

    manager.calls.clear()
    assert cli.run_engine(
        cli.build_parser().parse_args(["engine", "status"]),
        manager=manager,
        out=io.StringIO(),
        err=io.StringIO(),
    ) == 0
    assert manager.calls == ["status"]

    manager.calls.clear()
    assert cli.run_engine(
        cli.build_parser().parse_args(["engine", "health", "llama_cpp"]),
        manager=manager,
        out=io.StringIO(),
        err=io.StringIO(),
    ) == 0
    assert manager.calls == ["status"]

    manager.calls.clear()
    assert cli.run_engine(
        cli.build_parser().parse_args(["engine", "stop"]),
        manager=manager,
        out=io.StringIO(),
        err=io.StringIO(),
    ) == 0
    assert manager.calls == ["stop"]


def test_agent_uses_task_workflow_with_llama_cpp_chat_env(tmp_path, monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        def post_task(self, payload):
            self.calls.append(payload)
            return {"final_answer": "ok"}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RALF_CHAT_PROVIDER", "llama_cpp")
    client = Client()
    args = cli.build_parser().parse_args(["agent", "--yes", "goal"])
    assert cli.run_agent(args, client=client, out=io.StringIO(), err=io.StringIO()) == 0
    assert len(client.calls) == 1
    assert client.calls[0]["extra_context"]["terminal_client"]["provider"] == "llama_cpp"
    assert cli.config_from_args(args).provider == "llama_cpp"


def test_agent_explicit_llama_cpp_is_accepted_for_task_workflow(tmp_path, monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        def post_task(self, payload):
            self.calls.append(payload)
            return {"final_answer": "ok"}

    monkeypatch.chdir(tmp_path)
    client = Client()
    args = cli.build_parser().parse_args(["agent", "--provider", "llama_cpp", "--yes", "goal"])
    assert cli.run_agent(args, client=client, out=io.StringIO(), err=io.StringIO()) == 0
    assert len(client.calls) == 1
    assert client.calls[0]["extra_context"]["terminal_client"]["provider"] == "llama_cpp"


def test_llama_cpp_default_has_no_silent_ollama_fallback(monkeypatch):
    monkeypatch.delenv("RALF_LLAMA_CPP_FALLBACK", raising=False)
    assert LlamaCppServerConfig.from_env().fallback == "none"


def test_provider_sources_have_no_task_or_protected_action_paths():
    root = Path(__file__).resolve().parents[1]
    sources = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in (
            "ralfloop_agent/providers/llama_cpp.py",
            "ralfloop_agent/providers/llama_cpp_server.py",
        )
    )
    for forbidden in (
        "/tasks/run",
        "auto-approve",
        "auto-execute",
        "execute-approved",
        "promote_domain",
        "run_domain_canary",
        "apply_domain_source_update",
        "ngram-simple",
    ):
        assert forbidden not in sources


def test_launcher_is_user_space_and_has_no_global_kill():
    launcher = Path(__file__).resolve().parents[1] / "scripts" / "ralf_llama_cpp_engine.sh"
    source = launcher.read_text(encoding="utf-8")
    assert "ralfloop_agent.providers.llama_cpp_server" in source
    for forbidden in ("sudo", "systemctl", "killall", "pkill", "0.0.0.0"):
        assert forbidden not in source
    assert DEFAULT_MODEL_HASH not in source
