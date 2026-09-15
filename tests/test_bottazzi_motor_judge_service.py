from __future__ import annotations

from pathlib import Path

import pytest

from ralfloop_agent.integration.bottazzi_motor_judge_service import (
    ServiceConfigError,
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
    return binary, model, pack


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


def test_preflight_checks_artifacts(monkeypatch, tmp_path):
    _configure(monkeypatch, tmp_path)
    result = preflight()
    assert result["checks"]["allowed"] is True


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


def test_systemd_unit_is_restartable_and_gated():
    unit = Path("deploy/systemd/bottazzi-motor-judge.service").read_text()
    assert "EnvironmentFile=/etc/ralfloop/bottazzi-motor-judge.env" in unit
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
    assert "BOTTAZZI_MOTOR_AUDIT_PATH=/home/sibilla-cumana/" in env
    assert "/home/bandi/" not in env
