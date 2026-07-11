from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from ralfloop_agent.integration import recursive_mas_native as native
from ralfloop_agent.integration import recursive_mas_worker as worker


WORKER = Path("ralfloop_agent/integration/recursive_mas_worker.py").resolve()


def _fake_python(tmp_path: Path) -> Path:
    python = tmp_path / "python"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    return python


def test_worker_run_is_disabled_without_model_load():
    completed = subprocess.run(
        [sys.executable, str(WORKER), "--run"],
        input=json.dumps({"upstream_root": "/tmp/missing"}),
        text=True,
        capture_output=True,
        check=False,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["implemented"] is False
    assert payload["reason"] == "checkpoint_execution_not_enabled"
    assert payload["model_load_attempted"] is False


def test_worker_probe_returns_structured_json(tmp_path):
    root = tmp_path / "RecursiveMAS"
    root.mkdir()

    completed = subprocess.run(
        [sys.executable, str(WORKER), "--probe"],
        input=json.dumps({"upstream_root": str(root)}),
        text=True,
        capture_output=True,
        check=False,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["operation"] == "probe"
    assert payload["python_executable"] == sys.executable
    assert payload["model_load_attempted"] is False
    assert payload["download_attempted"] is False
    assert payload["runtime_ready"] is False


def test_adapter_worker_executable_missing(tmp_path):
    adapter = native.RecursiveMASNativeAdapter(root=tmp_path, python=tmp_path / "missing-python")

    result = adapter._call_worker("probe")

    assert result["ok"] is False
    assert result["error"] == "recursive_mas_python_missing"


def test_adapter_worker_timeout(monkeypatch, tmp_path):
    python = _fake_python(tmp_path)
    adapter = native.RecursiveMASNativeAdapter(root=tmp_path, python=python)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output="out", stderr="err")

    monkeypatch.setattr(native.subprocess, "run", fake_run)

    result = adapter._call_worker("probe", timeout_sec=1)

    assert result["ok"] is False
    assert result["error"] == "worker_timeout"


def test_adapter_rejects_stdout_that_is_not_json(monkeypatch, tmp_path):
    python = _fake_python(tmp_path)
    adapter = native.RecursiveMASNativeAdapter(root=tmp_path, python=python)

    class Completed:
        returncode = 0
        stdout = "not-json"
        stderr = ""

    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: Completed())

    result = adapter._call_worker("probe")

    assert result["ok"] is False
    assert result["error"].startswith("JSONDecodeError")


def test_adapter_handles_nonzero_exit_code(monkeypatch, tmp_path):
    python = _fake_python(tmp_path)
    adapter = native.RecursiveMASNativeAdapter(root=tmp_path, python=python)

    class Completed:
        returncode = 3
        stdout = json.dumps({"ok": False, "error": "boom"})
        stderr = "err"

    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: Completed())

    result = adapter._call_worker("probe")

    assert result["ok"] is False
    assert result["exit_code"] == 3
    assert result["json"]["error"] == "boom"


def test_adapter_worker_env_is_offline_and_drops_tokens(monkeypatch, tmp_path):
    python = _fake_python(tmp_path)
    adapter = native.RecursiveMASNativeAdapter(root=tmp_path, python=python)
    captured = {}
    monkeypatch.setenv("HF_TOKEN", "secret")
    monkeypatch.setenv("HUGGING_FACE_HUB_TOKEN", "secret")

    class Completed:
        returncode = 0
        stdout = json.dumps({"ok": True})
        stderr = ""

    def fake_run(*args, **kwargs):
        captured["env"] = kwargs["env"]
        captured["shell"] = kwargs["shell"]
        return Completed()

    monkeypatch.setattr(native.subprocess, "run", fake_run)

    result = adapter._call_worker("probe")

    assert result["ok"] is True
    assert captured["shell"] is False
    assert captured["env"]["HF_HUB_OFFLINE"] == "1"
    assert captured["env"]["TRANSFORMERS_OFFLINE"] == "1"
    assert captured["env"]["HF_DATASETS_OFFLINE"] == "1"
    assert "HF_TOKEN" not in captured["env"]
    assert "HUGGING_FACE_HUB_TOKEN" not in captured["env"]


def test_adapter_run_worker_uses_list_argv(monkeypatch, tmp_path):
    python = _fake_python(tmp_path)
    adapter = native.RecursiveMASNativeAdapter(root=tmp_path, python=python)
    captured = {}

    class Completed:
        returncode = 0
        stdout = json.dumps({"ok": True, "operation": "run", "implemented": False})
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return Completed()

    monkeypatch.setattr(native.subprocess, "run", fake_run)

    result = adapter._call_worker("run")

    assert result["ok"] is True
    assert isinstance(captured["argv"], list)
    assert captured["argv"][-1] == "--run"


def test_answer_parser_accepts_boxed_and_last_integer():
    boxed = worker._diagnose_answer(r"Therefore \boxed{5}", expected="5", generated_token_ids=[1, 2], max_new_tokens=256)
    plain = worker._diagnose_answer("The answer is 5.", expected="5", generated_token_ids=[1, 2], max_new_tokens=256)
    wrong = worker._diagnose_answer("2 + 3 = 6", expected="5", generated_token_ids=[1, 2], max_new_tokens=256)

    assert boxed["answer_correct"] is True
    assert boxed["format_compliant"] is True
    assert plain["answer_correct"] is True
    assert plain["format_compliant"] is False
    assert wrong["answer_correct"] is False
    assert wrong["last_integer"] == "6"


def test_answer_parser_detects_truncation_and_no_number():
    truncated = worker._diagnose_answer("To solve this", expected="5", generated_token_ids=list(range(16)), max_new_tokens=16)
    no_number = worker._diagnose_answer("No numeric answer", expected="5", generated_token_ids=[1], max_new_tokens=16)

    assert truncated["possibly_truncated"] is True
    assert "token_truncation" in truncated["diagnosis"]
    assert no_number["answer_correct"] is False
    assert no_number["last_integer"] is None


def test_generation_profiles_are_distinct():
    deterministic = worker._generation_profile("deterministic_diagnostic")
    release_like = worker._generation_profile("release_like")

    assert deterministic["do_sample"] is False
    assert deterministic["max_new_tokens"] == 256
    assert release_like["do_sample"] is True
    assert release_like["max_new_tokens"] == 1000
    assert release_like["latent_length"] == 32


def test_instrumentation_rounds_outer31_and_decode_counts():
    base_events = [
        {"kind": "inner", "link": "planner->planner", "source_agent": "planner"},
        {"kind": "inner", "link": "critic->critic", "source_agent": "critic"},
        {"kind": "outer", "link": "planner->critic", "source_agent": "planner"},
        {"kind": "outer", "link": "critic->solver", "source_agent": "critic"},
    ]
    one = worker._summarize_instrumentation(
        base_events,
        rounds=1,
        decode_counts={"decode_call_count": 1, "generate_call_count": 1, "intermediate_decode_count": 0, "final_decode_count": 1},
    )
    two_events = base_events * 2 + [
        {"kind": "inner", "link": "solver->solver", "source_agent": "solver"},
        {"kind": "outer", "link": "solver->planner", "source_agent": "solver"},
    ]
    two = worker._summarize_instrumentation(
        two_events,
        rounds=2,
        decode_counts={"decode_call_count": 1, "generate_call_count": 1, "intermediate_decode_count": 0, "final_decode_count": 1},
    )

    assert one["solver_to_planner_expected"] == 0
    assert one["closed_loop_verified"] is False
    assert two["solver_to_planner_expected"] == 1
    assert two["solver_to_planner_observed"] == 1
    assert two["closed_loop_verified"] is True
    assert two["single_final_decode"] is True


def test_native_diagnostic_matrix_requires_flag(monkeypatch):
    monkeypatch.delenv("RALFLOOP_RECURSIVE_MAS_NATIVE_DIAGNOSTIC", raising=False)

    result = worker._native_diagnostic_matrix(Path("/tmp/missing"), {"style": "sequential_light", "device": "cpu"})

    assert result["ok"] is False
    assert result["error"] == "native_diagnostic_disabled"


def test_adapter_accepts_native_diagnostic_matrix_operation(monkeypatch, tmp_path):
    python = _fake_python(tmp_path)
    adapter = native.RecursiveMASNativeAdapter(root=tmp_path, python=python)

    class Completed:
        returncode = 0
        stdout = json.dumps({"ok": True, "operation": "native-diagnostic-matrix"})
        stderr = ""

    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: Completed())

    result = adapter._call_worker("native-diagnostic-matrix")

    assert result["ok"] is True
    assert result["json"]["operation"] == "native-diagnostic-matrix"


def test_adapter_accepts_gpu_stagewise_operations(monkeypatch, tmp_path):
    python = _fake_python(tmp_path)
    adapter = native.RecursiveMASNativeAdapter(root=tmp_path, python=python)

    class Completed:
        returncode = 0
        stdout = json.dumps({"ok": True, "operation": "gpu-stagewise-probe"})
        stderr = ""

    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: Completed())

    assert adapter._call_worker("gpu-stagewise-probe")["ok"] is True
    assert adapter._call_worker("gpu-stagewise-canary")["ok"] is True
    assert adapter._call_worker("gpu-stagewise-matrix")["ok"] is True


def test_worker_env_propagates_gpu_stagewise_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE", "1")

    env = native._worker_env(tmp_path)

    assert env["RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE"] == "1"
    assert "HF_TOKEN" not in env


def test_stagewise_plan_unloads_between_roles_and_outer31_counts():
    one = worker._stagewise_event_plan(1)
    two = worker._stagewise_event_plan(2)
    three = worker._stagewise_event_plan(3)

    assert [item["role"] for item in one if item["event"] == "activate_stage"] == ["planner", "critic", "solver"]
    assert one[1]["event"] == "deactivate_stage"
    assert one[2]["role"] == "critic"
    assert sum(1 for item in one if item.get("outer") == "outer_31") == 0
    assert sum(1 for item in two if item.get("outer") == "outer_31") == 1
    assert sum(1 for item in three if item.get("outer") == "outer_31") == 2
    assert sum(1 for item in three if item.get("decode")) == 1


def test_vram_gate_and_leak_detector():
    assert worker._vram_gate_passed(100, 1000, 100) is True
    assert worker._vram_gate_passed(950, 1000, 100) is False
    assert worker._detect_cuda_leak(1000, 800, limit_bytes=100) is True
    assert worker._detect_cuda_leak(1000, 950, limit_bytes=100) is False


def test_stage_summary_requires_one_model_at_a_time():
    good = worker._summarize_stage_events([
        {"event": "activate_stage", "role": "planner", "one_model_active": True, "peak_allocated_bytes": 10},
        {"event": "deactivate_stage", "role": "planner", "memory_leak_detected": False},
    ])
    bad = worker._summarize_stage_events([
        {"event": "activate_stage", "role": "critic", "one_model_active": False, "peak_allocated_bytes": 20},
    ])

    assert good["one_model_at_a_time_verified"] is True
    assert good["vram_peak_by_role"]["planner"] == 10
    assert bad["one_model_at_a_time_verified"] is False


def test_gpu_stagewise_canary_requires_flag(monkeypatch):
    monkeypatch.delenv("RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE", raising=False)

    result = worker._gpu_stagewise_canary(Path("/tmp/missing"), {"device": "cuda:0"})

    assert result["ok"] is False
    assert result["error"] == "gpu_stagewise_disabled"


def test_gpu_stagewise_canary_rejects_cpu_without_fallback(monkeypatch):
    monkeypatch.setenv("RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE", "1")

    result = worker._gpu_stagewise_canary(Path("/tmp/missing"), {"device": "cpu"})

    assert result["ok"] is False
    assert result["error"] == "device_must_be_cuda"
    assert result["fallback_cpu_used"] is False


def test_gpu_stagewise_matrix_stops_after_failed_g1(monkeypatch):
    monkeypatch.setenv("RALFLOOP_RECURSIVE_MAS_GPU_STAGEWISE", "1")

    def fake_canary(root, request):
        return {"ok": False, "operation": "gpu-stagewise-canary", "rounds": request["rounds"], "cuda_oom": True}

    monkeypatch.setattr(worker, "_gpu_stagewise_canary", fake_canary)

    result = worker._gpu_stagewise_matrix(Path("/tmp/missing"), {"device": "cuda:0"})

    assert result["ok"] is False
    assert len(result["cases"]) == 1
    assert result["cases"][0]["rounds"] == 1


@pytest.mark.skipif(os.getenv("RALFLOOP_RECURSIVE_MAS_ENV_INTEGRATION") != "1", reason="RecursiveMAS env integration disabled")
def test_real_recursive_mas_worker_import_check_offline():
    python = Path("/home/sibilla-cumana/RecursiveMAS/.venv-recursivemas/bin/python")
    root = Path("/home/sibilla-cumana/RecursiveMAS")
    completed = subprocess.run(
        [str(python), str(WORKER), "--import-check"],
        input=json.dumps({"upstream_root": str(root)}),
        text=True,
        capture_output=True,
        env=native._worker_env(root),
        check=False,
        timeout=60,
    )

    payload = json.loads(completed.stdout)
    assert completed.returncode == 0
    assert payload["ok"] is True
    assert payload["offline_mode"] is True
    assert payload["download_attempted"] is False
    assert payload["model_load_attempted"] is False
