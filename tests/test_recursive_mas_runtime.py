from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from ralfloop_agent.integration import recursive_mas_runtime as runtime
from ralfloop_agent.integration.recursive_mas_runtime import (
    RecursiveMASRuntimeConfig,
    RecursiveMASRuntimeController,
)


def _cfg(tmp_path: Path, *, enabled: bool = True, timeout: int = 2, fallback: bool = False) -> RecursiveMASRuntimeConfig:
    root = tmp_path / "RecursiveMAS"
    cache = tmp_path / "cache"
    root.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    py = tmp_path / "python"
    py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    py.chmod(0o755)
    return RecursiveMASRuntimeConfig(
        enabled=enabled,
        allow_text_fallback=fallback,
        python_path=str(py),
        upstream_root=str(root),
        cache_path=str(cache),
        timeout_sec=timeout,
        lock_path=str(tmp_path / "runtime.lock"),
        circuit_path=str(tmp_path / "circuit.json"),
    )


def _success_worker() -> dict:
    return {
        "pid": 123,
        "exit_code": 0,
        "json": {
            "ok": True,
            "operation": "gpu-stagewise-canary",
            "native_latent_verified": True,
            "closed_loop_verified": True,
            "raw_answer": r"\\boxed{5}",
            "answer_correct": True,
            "format_compliant": True,
            "stage_summary": {"vram_peak_bytes": 99},
            "rss_peak_bytes": 88,
            "rounds": 1,
            "profile": "deterministic_diagnostic",
            "device": "cuda:0",
        },
        "orphan_detected": False,
    }


def test_native_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", raising=False)

    cfg = RecursiveMASRuntimeConfig.from_env()

    assert cfg.enabled is False
    assert cfg.allow_text_fallback is False
    assert cfg.execution_mode == "gpu_stagewise"


def test_config_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "1")
    monkeypatch.setenv("RALFLOOP_ALLOW_TEXT_MAS_FALLBACK", "1")
    monkeypatch.setenv("RALFLOOP_RECURSIVE_MAS_LOCK_PATH", str(tmp_path / "l-%UID%.lock"))
    monkeypatch.setenv("RALFLOOP_RECURSIVE_MAS_TIMEOUT_SEC", "7")

    cfg = RecursiveMASRuntimeConfig.from_env()

    assert cfg.enabled is True
    assert cfg.allow_text_fallback is True
    assert "%UID%" not in cfg.lock_path
    assert cfg.timeout_sec == 7


def test_lock_acquire_and_release(tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))

    acquired = controller.acquire("task-a")
    assert acquired["ok"] is True
    assert controller.status().busy is True

    controller.release()
    assert controller.status().busy is False


def test_lock_concurrent_returns_busy_and_no_second_worker(monkeypatch, tmp_path):
    holder = RecursiveMASRuntimeController(_cfg(tmp_path))
    holder.acquire("held")
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))
    called = {"popen": 0}

    def fake_popen(*args, **kwargs):
        called["popen"] += 1
        raise AssertionError("worker should not start")

    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)
    result = controller.execute({"goal": "What is 2 + 3?"})
    holder.release()

    assert result["status"] == "busy"
    assert result["retryable"] is True
    assert result["fallback_used"] is False
    assert called["popen"] == 0


def test_release_after_success(monkeypatch, tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(controller, "_run_worker", lambda *a, **k: _success_worker())

    result = controller.execute({"goal": "What is 2 + 3?", "rounds": 1})

    assert result["ok"] is True
    assert result["status"] == "completed"
    assert result["native_latent_verified"] is True
    assert controller.acquire("after")["ok"] is True
    controller.release()


def test_release_after_exception(monkeypatch, tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))
    monkeypatch.chdir(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(controller, "_run_worker", boom)
    result = controller.execute({"goal": "What is 2 + 3?"})

    assert result["ok"] is False
    assert result["error_type"] == "RuntimeError"
    assert controller.acquire("after")["ok"] is True
    controller.release()


class _TimeoutProc:
    pid = 456
    returncode = None

    def __init__(self, *args, **kwargs):
        self.calls = 0
        self.terminated = False
        self.killed = False
        self.kwargs = kwargs

    def communicate(self, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise subprocess.TimeoutExpired(cmd="worker", timeout=1)
        self.returncode = -15
        return "", "terminated"

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9

    def poll(self):
        return self.returncode


def test_timeout_sigterm_release_and_no_kill(monkeypatch, tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path, timeout=1))
    monkeypatch.chdir(tmp_path)
    proc = _TimeoutProc()
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: proc)

    result = controller.execute({"goal": "What is 2 + 3?"})

    assert result["status"] == "timeout"
    assert proc.terminated is True
    assert proc.killed is False
    assert result["cleanup_completed"] is True
    assert controller.acquire("after")["ok"] is True
    controller.release()


class _KillProc(_TimeoutProc):
    def communicate(self, *args, **kwargs):
        self.calls += 1
        if self.calls in (1, 2):
            raise subprocess.TimeoutExpired(cmd="worker", timeout=1)
        self.returncode = -9
        return "", "killed"


def test_sigkill_only_after_grace_timeout(monkeypatch, tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path, timeout=1))
    monkeypatch.chdir(tmp_path)
    proc = _KillProc()
    monkeypatch.setattr(runtime.subprocess, "Popen", lambda *a, **k: proc)

    result = controller.execute({"goal": "What is 2 + 3?"})

    assert result["status"] == "timeout"
    assert result["killed"] is True
    assert proc.killed is True


def test_worker_orphan_detected(monkeypatch, tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))
    monkeypatch.chdir(tmp_path)
    worker = _success_worker()
    worker["orphan_detected"] = True
    monkeypatch.setattr(controller, "_run_worker", lambda *a, **k: worker)

    result = controller.execute({"goal": "What is 2 + 3?"})

    assert result["orphan_detected"] is True
    assert result["cleanup_completed"] is False


def test_circuit_opens_after_three_errors_and_cooldown(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    controller = RecursiveMASRuntimeController(cfg)
    monkeypatch.chdir(tmp_path)
    bad = {"pid": 1, "exit_code": 2, "json": {"ok": False, "error": "bad"}, "orphan_detected": False}
    monkeypatch.setattr(controller, "_run_worker", lambda *a, **k: bad)

    for _ in range(3):
        controller.execute({"goal": "What is 2 + 3?"})
    status = controller.status()

    assert status.circuit_state == "open"
    assert status.consecutive_failures == 3
    blocked = controller.execute({"goal": "What is 2 + 3?"})
    assert blocked["status"] == "circuit_open"


def test_half_open_then_success_closes_circuit(monkeypatch, tmp_path):
    cfg = _cfg(tmp_path)
    controller = RecursiveMASRuntimeController(cfg)
    controller._write_circuit({**runtime._default_circuit(), "state": "open", "consecutive_failures": 3, "cooldown_until": time.time() - 1})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(controller, "_run_worker", lambda *a, **k: _success_worker())

    result = controller.execute({"goal": "What is 2 + 3?"})

    assert result["ok"] is True
    assert controller.status().circuit_state == "closed"
    assert controller.status().consecutive_failures == 0


def test_corrupt_circuit_file_is_preserved(tmp_path):
    cfg = _cfg(tmp_path)
    Path(cfg.circuit_path).write_text("not-json", encoding="utf-8")
    controller = RecursiveMASRuntimeController(cfg)

    status = controller.status()

    assert status.circuit_state == "closed"
    assert list(tmp_path.glob("circuit.json.corrupt-*"))


def test_atomic_circuit_write_leaves_no_tmp(tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))

    controller.reset_circuit()

    assert Path(controller.config.circuit_path).exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_fallback_disabled_default_and_explicit_fallback(tmp_path):
    no_fallback = RecursiveMASRuntimeController(_cfg(tmp_path / "a", fallback=False))
    fallback = RecursiveMASRuntimeController(_cfg(tmp_path / "b", fallback=True))
    Path(no_fallback.config.python_path).unlink()
    Path(fallback.config.python_path).unlink()

    a = no_fallback.execute({"goal": "What is 2 + 3?"})
    b = fallback.execute({"goal": "What is 2 + 3?"})

    assert a["ok"] is False
    assert a["fallback_used"] is False
    assert b["ok"] is True
    assert b["selected_backend"] == "text_mas_proxy"
    assert b["implementation_level"] == "text_proxy"
    assert b["native_latent_verified"] is False
    assert b["fallback_used"] is True


def test_no_fallback_when_confirmation_required(tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path, fallback=True))

    result = controller.execute({"goal": "send telegram", "jury_policy": {"requires_human_confirmation": True}})

    assert result["status"] == "human_confirmation_required"
    assert result["fallback_used"] is False


def test_audit_redacts_secrets(monkeypatch, tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(controller, "_run_worker", lambda *a, **k: _success_worker())

    controller.execute({"goal": "Authorization: secret Bearer token OPENAI_API_KEY=abc What is 2 + 3?"})
    audit = Path("logs/recursive_mas_runtime.jsonl").read_text(encoding="utf-8")

    assert "secret" not in audit
    assert " token" not in audit
    assert "abc" not in audit
    assert "request_hash" in audit


def test_worker_env_offline_no_tokens(monkeypatch, tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))
    monkeypatch.setenv("HF_TOKEN", "secret")

    env = controller._worker_env()

    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["TRANSFORMERS_OFFLINE"] == "1"
    assert "HF_TOKEN" not in env


def test_popen_uses_shell_false(monkeypatch, tmp_path):
    controller = RecursiveMASRuntimeController(_cfg(tmp_path))
    captured = {}

    class Proc:
        pid = 5
        returncode = 0
        def communicate(self, *a, **k):
            return json.dumps(_success_worker()["json"]), ""
        def poll(self):
            return self.returncode

    def fake_popen(*args, **kwargs):
        captured.update(kwargs)
        return Proc()

    monkeypatch.setattr(runtime.subprocess, "Popen", fake_popen)
    controller.execute({"goal": "What is 2 + 3?"})

    assert captured["shell"] is False


def test_cli_status_and_run_disabled(tmp_path):
    env = os.environ.copy()
    env.pop("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", None)
    env["RALFLOOP_RECURSIVE_MAS_CIRCUIT_PATH"] = str(tmp_path / "circuit.json")
    status = subprocess.run([sys.executable, "-m", "ralfloop_agent.integration.recursive_mas_runtime", "status"], env=env, text=True, capture_output=True, check=False)
    run = subprocess.run([sys.executable, "-m", "ralfloop_agent.integration.recursive_mas_runtime", "run", "--goal", "What is 2 + 3?"], env=env, text=True, capture_output=True, check=False)

    assert status.returncode == 0
    assert json.loads(status.stdout)["enabled"] is False
    assert run.returncode == 1
    assert json.loads(run.stdout)["status"] == "disabled"


def test_lab_api_status_and_error_codes(monkeypatch):
    import src.api as api

    class FakeController:
        def __init__(self, result):
            self.result = result
        def status(self):
            return type("S", (), {"to_dict": lambda self: {"enabled": False}})()
        def health(self):
            return {"ok": False}
        def execute(self, payload):
            return self.result

    client = TestClient(api.app)
    monkeypatch.setattr(api.RecursiveMASRuntimeController, "from_env", lambda: FakeController({"ok": False, "status": "busy"}))
    assert client.post("/lab/recursive-mas/run", json={"goal": "x"}).status_code == 409
    monkeypatch.setattr(api.RecursiveMASRuntimeController, "from_env", lambda: FakeController({"ok": False, "status": "disabled"}))
    assert client.post("/lab/recursive-mas/run", json={"goal": "x"}).status_code == 503
    monkeypatch.setattr(api.RecursiveMASRuntimeController, "from_env", lambda: FakeController({"ok": False, "status": "timeout"}))
    assert client.post("/lab/recursive-mas/run", json={"goal": "x"}).status_code == 504
    assert client.post("/lab/recursive-mas/run", json={"goal": "x", "python_path": "/bad"}).status_code == 422


def test_main_plugin_sha_unchanged_if_present():
    plugin = Path("/home/sibilla-cumana/gatto/cat/plugins/ralfloop_bridge/main_plugin.py")
    if not plugin.exists():
        pytest.skip("active Cheshire runtime not mounted")
    digest = subprocess.check_output(["sha256sum", str(plugin)], text=True).split()[0]
    assert digest == "ea941a7d8d33843fc8829561af72aa9e82129262c7f61ef8b95f7a153c1d21a6"
