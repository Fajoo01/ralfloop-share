from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import types

import pytest

from ralfloop_agent.integration import recursive_mas_native as native
from ralfloop_agent.integration import recursive_mas_worker as worker


def _snapshot(cache: Path, repo_id: str, revision: str = "abc123") -> Path:
    owner, name = repo_id.split("/", 1)
    path = cache / "hub" / f"models--{owner}--{name}" / "snapshots" / revision
    path.mkdir(parents=True)
    return path


def _write_required(path: Path, role: str) -> None:
    if role == "outer":
        for name in ["outerlink_config.json", *worker.SEQUENTIAL_OUTER_FILES.values()]:
            (path / name).write_text("{}", encoding="utf-8")
        return
    for name in (
        "config.json",
        "generation_config.json",
        "tokenizer.json",
        "tokenizer_config.json",
        "model.safetensors",
        "adapter(math).pt",
        "adapter_config.json",
        "innerlink_config.json",
    ):
        (path / name).write_text("x", encoding="utf-8")


def test_checkpoint_manifest_complete_mocked_cache(monkeypatch, tmp_path):
    cache = tmp_path / ".hf-recursivemas"
    monkeypatch.setenv("HF_HOME", str(cache))
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(cache / "hub"))
    for role, repo in worker.SEQUENTIAL_LIGHT_REPOS.items():
        _write_required(_snapshot(cache, repo, revision=f"rev-{role}"), role)

    manifest = worker._checkpoint_manifest(tmp_path, {"style": "sequential_light"})

    assert manifest["complete"] is True
    assert manifest["offline_ready"] is True
    assert manifest["resolved_revisions"]["planner"] == "rev-planner"
    assert manifest["repositories"]["outer"]["outerlinks"]["Planner-Critic"] is True


def test_checkpoint_manifest_detects_partial_and_missing_outerlink(monkeypatch, tmp_path):
    cache = tmp_path / ".hf-recursivemas"
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(cache / "hub"))
    for role, repo in worker.SEQUENTIAL_LIGHT_REPOS.items():
        snap = _snapshot(cache, repo)
        _write_required(snap, role)
    (snap / worker.SEQUENTIAL_OUTER_FILES["outer_31"]).unlink()

    manifest = worker._checkpoint_manifest(tmp_path, {"style": "sequential_light"})

    assert manifest["complete"] is False
    assert manifest["offline_ready"] is False
    missing = manifest["repositories"]["outer"]["missing_files"]
    assert worker.SEQUENTIAL_OUTER_FILES["outer_31"] in missing
    assert manifest["repositories"]["outer"]["outerlinks"]["Solver-Planner"] is False


def test_offline_guard_blocks_unauthorized_repo_and_returns_local_snapshot(monkeypatch, tmp_path):
    cache = tmp_path / ".hf-recursivemas"
    monkeypatch.setenv("HUGGINGFACE_HUB_CACHE", str(cache / "hub"))
    repo = worker.SEQUENTIAL_LIGHT_REPOS["planner"]
    _write_required(_snapshot(cache, repo, revision="fixed-rev"), "planner")
    calls = []

    def fake_snapshot_download(repo_id, *args, **kwargs):
        calls.append((repo_id, kwargs))
        return str(worker._snapshot_path_for_repo(repo_id))

    fake_hub = types.SimpleNamespace(snapshot_download=fake_snapshot_download)
    fake_resolver = types.SimpleNamespace(snapshot_download=fake_snapshot_download)
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hub)
    monkeypatch.setitem(sys.modules, "hf_resolver", fake_resolver)

    guard = worker._OfflineGuard(tmp_path)
    guard._patch_hf()
    resolved = fake_hub.snapshot_download(repo)

    assert calls == []
    assert resolved.endswith("/fixed-rev")
    with pytest.raises(RuntimeError):
        fake_hub.snapshot_download("RecursiveMAS/Sequential-Scaled-Forbidden")
    assert guard.download_attempted is True
    assert guard.unexpected_repo_request == ["RecursiveMAS/Sequential-Scaled-Forbidden"]


def test_worker_load_check_blocks_cuda():
    result = worker._load_check(Path("/tmp/missing"), {"style": "sequential_light", "device": "cuda"})

    assert result["ok"] is False
    assert result["error"] == "cuda_forbidden_for_canary"


def test_native_canary_requires_flag(monkeypatch):
    monkeypatch.delenv("RALFLOOP_RECURSIVE_MAS_NATIVE_CANARY", raising=False)

    result = worker._native_canary(Path("/tmp/missing"), {"style": "sequential_light", "device": "cpu"})

    assert result["ok"] is False
    assert result["error"] == "native_canary_disabled"


def test_native_canary_blocks_low_mem(monkeypatch):
    monkeypatch.setenv("RALFLOOP_RECURSIVE_MAS_NATIVE_CANARY", "1")
    monkeypatch.setattr(worker, "_mem_available_bytes", lambda: 1)

    result = worker._native_canary(Path("/tmp/missing"), {"style": "sequential_light", "device": "cpu"})

    assert result["ok"] is False
    assert result["error"] == "insufficient_memavailable_for_canary"


def test_native_worker_env_uses_dedicated_cache_and_drops_tokens(monkeypatch, tmp_path):
    root = tmp_path / "RecursiveMAS"
    monkeypatch.setenv("HF_TOKEN", "secret")
    env = native._worker_env(root)

    assert env["HF_HOME"] == str(root / ".hf-recursivemas")
    assert env["HUGGINGFACE_HUB_CACHE"] == str(root / ".hf-recursivemas" / "hub")
    assert env["HF_HUB_OFFLINE"] == "1"
    assert "HF_TOKEN" not in env


def test_main_plugin_sha_unchanged_if_present():
    plugin = Path("/home/sibilla-cumana/gatto/cat/plugins/ralfloop_bridge/main_plugin.py")
    if not plugin.exists():
        pytest.skip("active Cheshire plugin not present")

    digest = hashlib.sha256(plugin.read_bytes()).hexdigest()

    assert digest == "ea941a7d8d33843fc8829561af72aa9e82129262c7f61ef8b95f7a153c1d21a6"
