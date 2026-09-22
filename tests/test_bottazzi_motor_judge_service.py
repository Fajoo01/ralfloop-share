from __future__ import annotations

import os
from pathlib import Path

import pytest

from ralfloop_agent.integration import bottazzi_motor_judge_service as service
from ralfloop_agent.integration.bottazzi_motor_judge_service import (
    ServiceConfigError,
    acquire_resource_lease,
    build_exec,
    load_service_config,
    preflight,
)


def _configure(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    binary = tmp_path / "ds4-server"
    model = tmp_path / "model.gguf"
    pack = tmp_path / "experts.pack"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    model.write_bytes(b"model")
    pack.write_bytes(b"pack")
    monkeypatch.setenv("BOTTAZZI_MOTOR_JUDGE_BIN", str(binary))
    monkeypatch.setenv("BOTTAZZI_MOTOR_JUDGE_MODEL", str(model))
    monkeypatch.setenv("BOTTAZZI_MOTOR_JUDGE_PACK", str(pack))
    monkeypatch.setenv(
        "BOTTAZZI_MOTOR_JUDGE_RESOURCE_LOCK_FILE", str(tmp_path / "inference-gpu.lock")
    )
    return binary, model, pack


def _safe_resources(monkeypatch) -> None:
    monkeypatch.setattr(
        service,
        "resource_snapshot",
        lambda _cfg: {
            "mem_available_mb": 32768,
            "swap_free_mb": 4096,
            "gpu_free_mb": 7600,
            "ollama_models": [],
            "active_heavy_ports": [],
        },
    )


def test_defaults_match_validated_judge_profile(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    cfg = load_service_config()
    assert cfg["host"] == "127.0.0.1"
    assert cfg["port"] == 19196
    assert cfg["prefill_chunk"] == 128
    assert cfg["stage_mb"] == 768
    assert cfg["reserve_mb"] == 512
    assert cfg["expert_window"] == 32
    assert cfg["read_threads"] == 4
    assert cfg["host_cache_gb"] == 4
    assert cfg["host_cache_pinned"] is False
    assert cfg["expert_chunk_cache_gb"] == 8
    assert cfg["expert_chunk_cache_pinned"] is False
    assert cfg["min_available_ram_mb"] == 16384
    assert cfg["min_swap_free_mb"] == 1024
    assert cfg["min_gpu_free_mb"] == 6500
    assert cfg["deny_active_ports"] == (19194, 19195, 19240)


def test_preflight_checks_artifacts_and_resource_headroom(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    _safe_resources(monkeypatch)
    result = preflight()
    assert result["checks"]["allowed"] is True
    assert result["resources"]["gpu_free_mb"] == 7600


def test_reserved_production_port_is_rejected(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("BOTTAZZI_MOTOR_JUDGE_PORT", "19194")
    with pytest.raises(ServiceConfigError, match="reserved_production_port"):
        load_service_config()


def test_exec_plan_contains_stable_motor_settings(monkeypatch, tmp_path):
    binary, model, pack = _configure(monkeypatch, tmp_path)
    cfg = load_service_config()
    command, env = build_exec(cfg)
    assert command[0] == str(binary)
    assert command[command.index("-m") + 1] == str(model)
    assert command[command.index("--prefill-chunk") + 1] == "128"
    assert command[command.index("--port") + 1] == "19196"
    assert env["DS4_CUDA_LOW_VRAM_STAGE_MB"] == "768"
    assert env["DS4_CUDA_V41_EXPERT_PACK_FILE"] == str(pack)
    assert env["DS4_CUDA_V41_EXPERT_FRAME_DIRECT"] == "1"
    assert env["DS4_CUDA_LOW_VRAM_HOST_CACHE_GB"] == "4"
    assert env["DS4_CUDA_LOW_VRAM_HOST_CACHE_PINNED"] == "0"
    assert env["DS4_CUDA_HOST_EXPERT_CHUNK_CACHE_GB"] == "8"
    assert env["DS4_CUDA_HOST_EXPERT_CHUNK_CACHE_PINNED"] == "0"


def test_systemd_unit_is_restartable_and_gated():
    unit = Path("deploy/systemd/bottazzi-motor-judge.service").read_text()
    assert "EnvironmentFile=/etc/ralfloop/bottazzi-motor-judge.env" in unit
    assert "WorkingDirectory=/home/sibilla-cumana/ralfloop-motor-judge/current" in unit
    assert "PYTHONPATH=/home/sibilla-cumana/ralfloop-motor-judge/current" in unit
    assert "/home/sibilla-cumana/ralfloop-production/current" not in unit
    assert "bottazzi_motor_judge_service preflight" in unit
    assert "bottazzi_motor_judge_service wait-ready --timeout 180" in unit
    assert "Restart=on-failure" in unit
    assert "KillMode=mixed" in unit


def test_backend_dropin_shares_bounded_judge_environment():
    dropin = Path("deploy/systemd/ralfloop-backend-bottazzi-motor-judge.conf").read_text()
    env = Path("deploy/systemd/bottazzi-motor-judge.env.example").read_text()
    assert "EnvironmentFile=/etc/ralfloop/bottazzi-motor-judge.env" in dropin
    assert "BOTTAZZI_MOTOR_MAX_TOKENS=64" in env
    assert "BOTTAZZI_MOTOR_JUDGE_TOKENS=64" in env
    assert "BOTTAZZI_MOTOR_JUDGE_HOST_CACHE_PINNED=0" in env
    assert "BOTTAZZI_MOTOR_JUDGE_MIN_SWAP_FREE_MB=1024" in env
    assert "BOTTAZZI_MOTOR_JUDGE_RESOURCE_LOCK_FILE=/run/ralfloop/inference-gpu.lock" in env
    assert "BOTTAZZI_MOTOR_AUDIT_PATH=/home/sibilla-cumana/" in env
    assert "/home/bandi/" not in env


def test_dense_readahead_is_disabled_by_default(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    cfg = load_service_config()
    command, env = build_exec(cfg)
    assert command
    assert cfg["dense_readahead"] is False
    assert env["DS4_CUDA_LOW_VRAM_DENSE_READAHEAD"] == "0"


def test_dense_readahead_requires_explicit_opt_in(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("BOTTAZZI_MOTOR_JUDGE_DENSE_READAHEAD", "1")
    cfg = load_service_config()
    _, env = build_exec(cfg)
    assert cfg["dense_readahead"] is True
    assert env["DS4_CUDA_LOW_VRAM_DENSE_READAHEAD"] == "1"


def test_dense_readahead_rejects_invalid_boolean(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("BOTTAZZI_MOTOR_JUDGE_DENSE_READAHEAD", "maybe")
    with pytest.raises(ServiceConfigError, match="invalid_boolean"):
        load_service_config()



def test_preflight_blocks_resource_pressure(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setattr(
        service,
        "resource_snapshot",
        lambda _cfg: {
            "mem_available_mb": 12000,
            "swap_free_mb": 128,
            "gpu_free_mb": 5000,
            "ollama_models": ["qwen3.5:9b"],
            "active_heavy_ports": [19194],
        },
    )
    result = preflight()
    assert result["checks"]["allowed"] is False
    assert result["checks"]["memory_headroom"] is False
    assert result["checks"]["swap_headroom"] is False
    assert result["checks"]["gpu_headroom"] is False
    assert result["checks"]["ollama_idle"] is False
    assert result["checks"]["heavy_ports_idle"] is False


def test_resource_lease_is_exclusive(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    cfg = load_service_config()
    first_fd = acquire_resource_lease(cfg)
    try:
        with pytest.raises(ServiceConfigError, match="resource_lease_busy"):
            acquire_resource_lease(cfg)
    finally:
        os.close(first_fd)


def test_invalid_deny_ports_are_rejected(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    monkeypatch.setenv("BOTTAZZI_MOTOR_JUDGE_DENY_ACTIVE_PORTS", "19194,nope")
    with pytest.raises(ServiceConfigError, match="invalid_ports"):
        load_service_config()
