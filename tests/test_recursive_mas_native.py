from __future__ import annotations

import os
import builtins

from ralfloop_agent.integration import recursive_mas_native as native
from ralfloop_agent.integration.recursive_mas_native import (
    RecursiveMASNativeAdapter,
    RecursiveMASProbeResult,
)


def _fake_repo(tmp_path, complete: bool = False):
    root = tmp_path / "RecursiveMAS"
    inference = root / "inference"
    utils = inference / "inference_utils"
    utils.mkdir(parents=True)
    if complete:
        (root / "README.md").write_text("RecursiveMAS\n", encoding="utf-8")
        (root / "requirements.txt").write_text("torch==2.9.0\n", encoding="utf-8")
        (inference / "README.md").write_text("Inference\n", encoding="utf-8")
        (inference / "run.py").write_text("# run\n", encoding="utf-8")
        (inference / "hf_resolver.py").write_text("# resolver\n", encoding="utf-8")
        (inference / "load_from_repo.py").write_text("# load specs\n", encoding="utf-8")
        (utils / "inference_mas.py").write_text("# latent inputs_embeds hidden_states\n", encoding="utf-8")
        (utils / "inference_mas_mixture.py").write_text("# mixture\n", encoding="utf-8")
        (utils / "inference_mas_distill.py").write_text("# distill\n", encoding="utf-8")
        (utils / "inference_mas_deliberation.py").write_text("# deliberation\n", encoding="utf-8")
        (utils / "llm_judge.py").write_text("# llm judge\n", encoding="utf-8")
    (inference / "system_loader.py").write_text("def load_mas_system(*a, **k): return object()\n", encoding="utf-8")
    (inference / "modeling.py").write_text(
        "class Adapter:\n    def forward(self, x: object): return x\n"
        "class CrossModelAdapter:\n    def __init__(self): self.residual_proj = None\n",
        encoding="utf-8",
    )
    return root


def test_probe_handles_repository_missing(tmp_path):
    adapter = RecursiveMASNativeAdapter(root=tmp_path / "missing")

    result = adapter.probe()

    assert result.available is False
    assert result.native_latent is False
    assert "repository_missing" in result.reason
    assert result.repository_status == "missing"
    assert result.runtime_ready is False


def test_probe_handles_dependencies_missing(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path)
    monkeypatch.setattr(native, "_dependency_info", lambda: {
        "torch": {"importable": False, "version": None},
        "transformers": {"importable": False, "version": None},
    })
    monkeypatch.setattr(native, "_checkpoints_available", lambda style: True)

    result = RecursiveMASNativeAdapter(root=root).probe()

    assert result.available is False
    assert "torch_missing" in result.reason
    assert "transformers_missing" in result.reason
    assert result.dependencies_available is False


def test_probe_handles_cuda_absent(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path)
    monkeypatch.setattr(native, "_dependency_info", lambda: {
        "torch": {"importable": True, "version": "test"},
        "transformers": {"importable": True, "version": "test"},
    })
    monkeypatch.setattr(native, "_cuda_info", lambda device=None: {"available": False, "device": device or "cpu"})
    monkeypatch.setattr(native, "_checkpoints_available", lambda style: True)

    result = RecursiveMASNativeAdapter(root=root).probe(device="cpu")

    assert result.cuda["available"] is False
    assert result.cuda_probe_status == "unavailable"
    assert result.inner_recursive_link_present is True
    assert result.outer_recursive_link_present is True


def test_probe_does_not_load_models(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path)
    adapter = RecursiveMASNativeAdapter(root=root)

    adapter.probe()

    assert adapter._system is None


def test_probe_uses_current_python_executable(tmp_path):
    result = RecursiveMASNativeAdapter(root=tmp_path / "missing").probe()

    assert result.python_executable
    assert result.python_executable == native.sys.executable
    assert isinstance(result.virtualenv_active, bool)


def test_meminfo_uses_memavailable(monkeypatch):
    monkeypatch.setattr(
        native,
        "_meminfo",
        lambda: {
            "MemTotal": 100,
            "MemFree": 10,
            "MemAvailable": 80,
            "Buffers": 3,
            "Cached": 40,
            "SwapTotal": 50,
            "SwapFree": 45,
        },
    )

    ram = native._ram_info()

    assert ram["free_bytes"] == 10
    assert ram["available_bytes"] == 80
    assert ram["cached_bytes"] == 40
    assert ram["source"] == "/proc/meminfo"


def test_ram_fallback_when_proc_meminfo_unavailable(monkeypatch):
    monkeypatch.setattr(native, "_meminfo", lambda: {})
    monkeypatch.setattr(native.os, "sysconf", lambda key: {"SC_PHYS_PAGES": 10, "SC_AVPHYS_PAGES": 4, "SC_PAGE_SIZE": 1024}[key])

    ram = native._ram_info()

    assert ram["total_bytes"] == 10240
    assert ram["available_bytes"] == 4096
    assert ram["source"] == "sysconf_fallback"


def test_fits_cpu_uses_available_not_free(monkeypatch):
    monkeypatch.setattr(native, "_cuda_info", lambda device=None: {"status": "unavailable", "available": False})
    monkeypatch.setattr(
        native,
        "_ram_info",
        lambda: {
            "available_bytes": 20_000_000_000,
            "free_bytes": 1,
            "source": "test",
        },
    )

    estimate = RecursiveMASNativeAdapter(root="/tmp/nope").estimate_resources("sequential_light", dtype="float16")

    assert estimate["fits_cpu"] is True
    assert estimate["available_ram_bytes"] == 20_000_000_000


def test_dtype_changes_estimate(monkeypatch):
    monkeypatch.setattr(native, "_cuda_info", lambda device=None: {"status": "unavailable", "available": False})
    monkeypatch.setattr(native, "_ram_info", lambda: {"available_bytes": 100_000_000_000})
    adapter = RecursiveMASNativeAdapter(root="/tmp/nope")

    fp16 = adapter.estimate_resources("sequential_light", dtype="float16")
    fp32 = adapter.estimate_resources("sequential_light", dtype="float32")
    bf16 = adapter.estimate_resources("sequential_light", dtype="bfloat16")

    assert fp32["weights_bytes"] == fp16["weights_bytes"] * 2
    assert bf16["weights_bytes"] == fp16["weights_bytes"]
    assert fp16["dtype"] == "float16"


def test_cuda_dependency_missing_status(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "torch":
            raise ModuleNotFoundError("No module named 'torch'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    cuda = native._cuda_info(device="cuda")

    assert cuda["status"] == "dependency_missing"


def test_repository_partial_and_complete(tmp_path):
    partial = RecursiveMASNativeAdapter(root=_fake_repo(tmp_path / "partial")).probe()
    complete_root = _fake_repo(tmp_path / "complete", complete=True)
    complete = native._repository_info(complete_root)

    assert partial.repository_status == "partial"
    assert complete["repository_status"] == "complete"
    assert not complete["upstream_files_missing"]


def test_checkpoint_missing_partial_complete(monkeypatch, tmp_path):
    hub = tmp_path / "hub"
    monkeypatch.setattr(native, "_checkpoint_search_paths", lambda: [hub])

    missing = native._checkpoint_info("sequential_light")
    assert missing["checkpoint_status"] == "missing"

    owner, name = native.SEQUENTIAL_LIGHT_REPOS[0].split("/", 1)
    (hub / f"models--{owner}--{name}").mkdir(parents=True)
    partial = native._checkpoint_info("sequential_light")
    assert partial["checkpoint_status"] == "partial"

    for repo in native.SEQUENTIAL_LIGHT_REPOS:
        owner, name = repo.split("/", 1)
        (hub / f"models--{owner}--{name}").mkdir(parents=True, exist_ok=True)
    complete = native._checkpoint_info("sequential_light")
    assert complete["checkpoint_status"] == "complete"


def test_runtime_ready_requires_all_prerequisites(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path, complete=True)
    monkeypatch.setenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "1")
    monkeypatch.setattr(native, "_dependency_info", lambda: {
        "torch": {"importable": True, "version": "x"},
        "transformers": {"importable": True, "version": "x"},
        "huggingface_hub": {"importable": True, "version": "x"},
    })
    monkeypatch.setattr(native, "_checkpoint_info", lambda style: {
        "checkpoint_search_paths": [],
        "checkpoint_files_found": [],
        "checkpoint_manifest": {},
        "checkpoint_status": "complete",
    })
    monkeypatch.setattr(RecursiveMASNativeAdapter, "estimate_resources", lambda self, style, dtype="float16", device=None: {
        "fits_cpu": True,
        "fits_cuda": False,
    })

    result = RecursiveMASNativeAdapter(root=root).probe(device="cpu")

    assert result.runtime_ready is True
    assert result.available is True


def test_preflight_blocks_cuda_load_over_vram(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path)
    adapter = RecursiveMASNativeAdapter(root=root)
    monkeypatch.setenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "1")
    monkeypatch.setattr(
        adapter,
        "probe",
        lambda style="sequential_light", device=None: RecursiveMASProbeResult(
            available=True,
            native_latent=True,
            repository=str(root),
            upstream_commit="abc",
            python_version="3",
            checkpoints_available=True,
            system_loader_present=True,
            inner_recursive_link_present=True,
            outer_recursive_link_present=True,
            reason="available",
        ),
    )
    monkeypatch.setattr(
        adapter,
        "estimate_resources",
        lambda style, dtype="float16", device=None: {
            "estimated_weights_bytes": 1,
            "estimated_runtime_bytes": 10,
            "available_vram_bytes": 1,
            "available_ram_bytes": 100,
            "fits_cuda": False,
            "fits_cpu": True,
            "reason": "test",
        },
    )

    result = adapter.load(style="sequential_light", device="cuda", dtype="float16")

    assert result.executed is False
    assert result.available is False
    assert result.error_type == "insufficient_vram"


def test_load_sets_offline_mode_before_official_loader(monkeypatch, tmp_path):
    root = _fake_repo(tmp_path)
    adapter = RecursiveMASNativeAdapter(root=root)
    monkeypatch.setenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "1")
    monkeypatch.delenv("RALFLOOP_RECURSIVE_MAS_ALLOW_DOWNLOAD", raising=False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    monkeypatch.setattr(
        adapter,
        "probe",
        lambda style="sequential_light", device=None: RecursiveMASProbeResult(
            available=True,
            native_latent=True,
            repository=str(root),
            upstream_commit="abc",
            python_version="3",
            checkpoints_available=True,
            system_loader_present=True,
            inner_recursive_link_present=True,
            outer_recursive_link_present=True,
            reason="available",
        ),
    )
    monkeypatch.setattr(
        adapter,
        "estimate_resources",
        lambda style, dtype="float16", device=None: {
            "estimated_weights_bytes": 1,
            "estimated_runtime_bytes": 1,
            "available_vram_bytes": 0,
            "available_ram_bytes": 100,
            "fits_cuda": False,
            "fits_cpu": True,
            "reason": "test",
        },
    )

    adapter.load(style="sequential_light", device="cpu", dtype="float16")

    assert os.environ["HF_HUB_OFFLINE"] == "1"
    assert os.environ["TRANSFORMERS_OFFLINE"] == "1"


def test_native_latent_true_only_after_mock_native_execution():
    adapter = RecursiveMASNativeAdapter(root="/tmp/nope")
    adapter._system = object()
    adapter._inner_outer_verified = True
    adapter._native_runner = lambda system, prompt, rounds: "ok"

    result = adapter.run("prompt", recursion_rounds=3)

    assert result.executed is True
    assert result.implementation_level == "native_latent"
    assert result.native_latent is True
    assert result.answer == "ok"


def test_run_without_native_system_is_not_success():
    result = RecursiveMASNativeAdapter(root="/tmp/nope").run("prompt")

    assert result.executed is False
    assert result.native_latent is False
    assert result.error_type == "not_loaded"


def test_resource_estimate_is_prudent(monkeypatch):
    monkeypatch.setattr(native, "_cuda_info", lambda device=None: {"available": True, "free_bytes": 8_000_000_000})
    monkeypatch.setattr(native, "_ram_info", lambda: {"available_bytes": 64_000_000_000})

    result = RecursiveMASNativeAdapter(root="/tmp/nope").estimate_resources("sequential_light", dtype="float16", device="cuda")

    assert result["estimated_weights_bytes"] > 8_000_000_000
    assert result["fits_cuda"] is False
    assert result["fits_cpu"] is True
