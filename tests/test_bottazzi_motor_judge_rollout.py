from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools import deploy_bottazzi_motor_judge_release as rollout


def _fake_release(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "releases"
    release = root / "abc123"
    module = release / "ralfloop_agent/integration/bottazzi_motor_judge_service.py"
    unit = release / "deploy/systemd/bottazzi-motor-judge.service"
    module.parent.mkdir(parents=True)
    unit.parent.mkdir(parents=True)
    module.write_text("x = 1\n", encoding="utf-8")
    unit.write_text(
        "WorkingDirectory=/home/sibilla-cumana/ralfloop-motor-judge/current\n"
        "Environment=PYTHONPATH=/home/sibilla-cumana/ralfloop-motor-judge/current\n",
        encoding="utf-8",
    )
    (release / "RELEASE.json").write_text(
        json.dumps({"commit": "abc123"}) + "\n", encoding="utf-8"
    )
    files = [module, unit, release / "RELEASE.json"]
    (release / "MANIFEST.sha256").write_text(
        "\n".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.relative_to(release)}"
            for path in files
        ) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(rollout, "RELEASE_ROOT", root)
    return release


def test_force_env_value_replaces_or_appends():
    source = "A=1\nBOTTAZZI_MOTOR_JUDGE_DENSE_READAHEAD=1\n"
    replaced = rollout._force_env_value(
        source, "BOTTAZZI_MOTOR_JUDGE_DENSE_READAHEAD", "0"
    )
    assert "DENSE_READAHEAD=0" in replaced
    assert "DENSE_READAHEAD=1" not in replaced
    appended = rollout._force_env_value("A=1\n", "B", "2")
    assert appended.endswith("B=2\n")


def test_release_requires_dedicated_judge_code_root(tmp_path, monkeypatch):
    release = _fake_release(tmp_path, monkeypatch)
    assert rollout.validate_release(release)["allowed"] is True
    unit = release / "deploy/systemd/bottazzi-motor-judge.service"
    unit.write_text(
        "WorkingDirectory=/home/sibilla-cumana/ralfloop-production/current\n",
        encoding="utf-8",
    )
    assert rollout.validate_release(release)["dedicated_code_root"] is False


def test_main_is_plan_only_without_apply(monkeypatch, capsys):
    called = {"apply": False}
    monkeypatch.setattr(rollout, "rollout_plan", lambda release: {
        "release_checks": {"allowed": True},
        "candidate_preflight": True,
        "production_19194_pid": 10,
    })

    def forbidden_apply(release):
        called["apply"] = True
        raise AssertionError("apply must be explicit")

    monkeypatch.setattr(rollout, "apply_release", forbidden_apply)
    assert rollout.main(["/tmp/candidate"]) == 0
    assert called["apply"] is False
    assert "candidate_preflight" in capsys.readouterr().out


def test_apply_rolls_back_when_canary_fails(tmp_path, monkeypatch):
    release = tmp_path / "candidate"
    old = tmp_path / "old"
    judge_root = tmp_path / "judge"
    release.mkdir()
    old.mkdir()
    monkeypatch.setattr(rollout.os, "geteuid", lambda: 0)
    monkeypatch.setattr(rollout, "JUDGE_ROOT", judge_root)
    monkeypatch.setattr(rollout, "JUDGE_CURRENT", judge_root / "current")
    monkeypatch.setattr(rollout, "JUDGE_PREVIOUS", judge_root / "previous")
    monkeypatch.setattr(rollout, "PRODUCTION_CURRENT", old)
    monkeypatch.setattr(rollout, "validate_release", lambda release: {"allowed": True})
    monkeypatch.setattr(rollout, "candidate_preflight", lambda release: True)
    monkeypatch.setattr(
        rollout, "_listener_pid",
        lambda port: 111 if port == rollout.PRODUCTION_DS4_PORT else 222,
    )
    backup = tmp_path / "backup"
    backup.mkdir()
    monkeypatch.setattr(rollout, "_backup_state", lambda target: backup)
    monkeypatch.setattr(rollout, "_install_candidate_config", lambda release: None)
    monkeypatch.setattr(rollout, "_systemctl", lambda *args, **kwargs: None)
    monkeypatch.setattr(rollout, "_wait_service_active", lambda: True)
    monkeypatch.setattr(
        rollout, "run_canaries",
        lambda env: [{"label": "small", "tokens": 180, "ok": False}],
    )
    monkeypatch.setattr(rollout, "_candidate_env", lambda release: {})
    rolled_back = {"value": False}

    def fake_rollback(backup_path, old_target):
        rolled_back["value"] = True
        assert backup_path == backup
        assert old_target == old.resolve()

    monkeypatch.setattr(rollout, "_rollback", fake_rollback)
    result = rollout.apply_release(release)
    assert result["applied"] is False
    assert result["rolled_back"] is True
    assert rolled_back["value"] is True
    assert result["production_19194_unchanged"] is True
