from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from tools import deploy_local_arch_release as deploy
from tools.deploy_local_arch_release import evaluate_quality_gate


def legacy_gate() -> dict[str, object]:
    baseline_ids = [f"tests.test_legacy::test_{index}" for index in range(61)]
    candidate_ids = baseline_ids[:53]
    return {
        "scoped_tests": True,
        "sandbox": True,
        "no_host_candidate_execution": True,
        "benchmark_minimum": True,
        "functiongemma_real": True,
        "runtime_canary": "pass",
        "policy_bypass": 0,
        "approval_miss": 0,
        "enough_ram": True,
        "enough_swap": True,
        "functiongemma_endpoint": True,
        "full_suite_clean": False,
        "preexisting_failures_only": True,
        "no_regression": True,
        "new_failures": 0,
        "baseline_suite": {"tests": 1331, "failures": 61, "errors": 0},
        "candidate_suite": {"tests": 1474, "failures": 53, "errors": 0},
        "baseline_failure_ids": baseline_ids,
        "candidate_failure_ids": candidate_ids,
        "suite_report_complete": True,
        "suite_exit_preexisting_same": True,
    }


def assert_blocked(data: dict[str, object]) -> None:
    assert evaluate_quality_gate(data)[0] is False


def test_full_clean_passes() -> None:
    data = legacy_gate()
    data["full_suite_clean"] = True
    data["preexisting_failures_only"] = False
    assert evaluate_quality_gate(data) == (True, "full_suite_clean", [])


def test_preexisting_failures_only_passes() -> None:
    assert evaluate_quality_gate(legacy_gate()) == (True, "preexisting_failures_only", [])


def test_one_new_failure_blocks() -> None:
    data = legacy_gate()
    data["new_failures"] = 1
    data["no_regression"] = False
    assert_blocked(data)


def test_same_count_with_different_identity_blocks() -> None:
    data = legacy_gate()
    candidate_ids = data["candidate_failure_ids"]
    assert isinstance(candidate_ids, list)
    data["candidate_failure_ids"] = candidate_ids[:-1] + ["tests.test_new::test_failure"]
    assert_blocked(data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("scoped_tests", False),
        ("functiongemma_real", False),
        ("policy_bypass", 1),
        ("approval_miss", 1),
        ("enough_swap", False),
        ("suite_exit_preexisting_same", False),
        ("suite_report_complete", False),
    ],
)
def test_required_legacy_evidence_blocks(field: str, value: object) -> None:
    data = legacy_gate()
    data[field] = value
    assert_blocked(data)


def test_candidate_error_blocks() -> None:
    data = deepcopy(legacy_gate())
    candidate = data["candidate_suite"]
    assert isinstance(candidate, dict)
    candidate["errors"] = 1
    assert_blocked(data)


def test_distributed_endpoint_requires_owned_proxy(monkeypatch) -> None:
    monkeypatch.setenv("RALF_FUNCTIONGEMMA_PROXY_EXPECTED", "1")
    monkeypatch.setattr(deploy, "_functiongemma_proxy_listener_owned", lambda: False)
    assert deploy.functiongemma_endpoint_check() is False


def test_distributed_endpoint_accepts_owned_healthy_proxy(monkeypatch) -> None:
    monkeypatch.setenv("RALF_FUNCTIONGEMMA_PROXY_EXPECTED", "1")
    monkeypatch.setattr(deploy, "_functiongemma_proxy_listener_owned", lambda: True)

    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr(deploy, "urlopen", lambda *args, **kwargs: Response())
    assert deploy.functiongemma_endpoint_check() is True


def _release(root: Path, directory: str, commit: str) -> Path:
    release = root / "releases" / directory
    gate = release / ".ralf_run/local_arch_v1/gates.json"
    gate.parent.mkdir(parents=True)
    metadata = release / "RELEASE.json"
    metadata.write_text(json.dumps({"commit": commit}), encoding="utf-8")
    gate_data = legacy_gate()
    gate_data["tested_commit"] = commit
    gate.write_text(json.dumps(gate_data), encoding="utf-8")
    rows = []
    for path in (metadata, gate):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        rows.append(f"{digest}  {path.relative_to(release)}")
    (release / "MANIFEST.sha256").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return release


def _allow_preflight(monkeypatch, production: Path, model: Path) -> None:
    monkeypatch.setattr(deploy, "PRODUCTION", production)
    monkeypatch.setattr(deploy, "MODEL", model)
    monkeypatch.setattr(deploy, "port_free", lambda port: True)
    monkeypatch.setattr(deploy, "process_match", lambda needles: False)
    monkeypatch.setattr(
        deploy,
        "meminfo",
        lambda: {"MemAvailable": 8192, "SwapFree": 1024},
    )




def test_gate_marker_rejects_wrong_tested_commit(tmp_path, monkeypatch) -> None:
    production = tmp_path / "production"
    candidate = _release(production, "c" * 40, "c" * 40)
    gate = candidate / ".ralf_run/local_arch_v1/gates.json"
    data = json.loads(gate.read_text())
    data["tested_commit"] = "d" * 40
    gate.write_text(json.dumps(data), encoding="utf-8")
    allowed, path, blockers = deploy.gate_marker_details(candidate)
    assert allowed is False
    assert path is None
    assert blockers == ["gate_commit_mismatch"]

def test_publish_rejects_release_commit_directory_mismatch(tmp_path, monkeypatch) -> None:
    production = tmp_path / "production"
    old = _release(production, "a" * 40, "a" * 40)
    candidate = _release(production, "b" * 40, "c" * 40)
    (production / "current").symlink_to(old)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    _allow_preflight(monkeypatch, production, model)

    result = deploy.publish(candidate)

    assert result["published"] is False
    assert result["checks"]["release_identity"] is False
    assert (production / "current").resolve() == old.resolve()
    assert not (production / "previous").exists()


def test_publish_preserves_valid_previous_when_current_identity_is_invalid(
    tmp_path,
    monkeypatch,
) -> None:
    production = tmp_path / "production"
    current = _release(production, "a" * 40, "d" * 40)
    previous = _release(production, "b" * 40, "b" * 40)
    candidate = _release(production, "c" * 40, "c" * 40)
    (production / "current").symlink_to(current)
    (production / "previous").symlink_to(previous)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    _allow_preflight(monkeypatch, production, model)
    current_integrity = deploy.rollback_integrity(current)

    result = deploy.publish(candidate)

    assert current_integrity["release_identity"] is False
    assert current_integrity["manifest"] is True
    assert result["published"] is True
    assert result["rollback_source"] == "previous"
    assert result["rollback_integrity"]["allowed"] is True
    assert (production / "current").resolve() == candidate.resolve()
    assert (production / "previous").resolve() == previous.resolve()


def test_publish_preserves_valid_previous_when_current_manifest_is_invalid(
    tmp_path,
    monkeypatch,
) -> None:
    production = tmp_path / "production"
    current = _release(production, "a" * 40, "a" * 40)
    previous = _release(production, "b" * 40, "b" * 40)
    candidate = _release(production, "c" * 40, "c" * 40)
    (current / ".ralf_run/local_arch_v1/gates.json").write_text(
        "{}",
        encoding="utf-8",
    )
    (production / "current").symlink_to(current)
    (production / "previous").symlink_to(previous)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    _allow_preflight(monkeypatch, production, model)
    current_integrity = deploy.rollback_integrity(current)

    result = deploy.publish(candidate)

    assert current_integrity["release_identity"] is True
    assert current_integrity["manifest"] is False
    assert result["published"] is True
    assert result["rollback_source"] == "previous"
    assert (production / "current").resolve() == candidate.resolve()
    assert (production / "previous").resolve() == previous.resolve()


def test_publish_blocks_when_no_integrity_checked_rollback_exists(
    tmp_path,
    monkeypatch,
) -> None:
    production = tmp_path / "production"
    current = _release(production, "a" * 40, "d" * 40)
    previous = _release(production, "b" * 40, "e" * 40)
    candidate = _release(production, "c" * 40, "c" * 40)
    (production / "current").symlink_to(current)
    (production / "previous").symlink_to(previous)
    model = tmp_path / "model.gguf"
    model.write_bytes(b"model")
    _allow_preflight(monkeypatch, production, model)

    result = deploy.publish(candidate)

    assert result["published"] is False
    assert result["reason"] == "rollback_release_unavailable"
    assert result["rollback_integrity"]["allowed"] is False
    assert (production / "current").resolve() == current.resolve()
    assert (production / "previous").resolve() == previous.resolve()


def test_rollback_drill_uses_valid_previous_when_current_is_invalid(
    tmp_path,
    monkeypatch,
) -> None:
    production = tmp_path / "production"
    current = _release(production, "a" * 40, "d" * 40)
    previous = _release(production, "b" * 40, "b" * 40)
    candidate = _release(production, "c" * 40, "c" * 40)
    (production / "current").symlink_to(current)
    (production / "previous").symlink_to(previous)
    monkeypatch.setattr(deploy, "PRODUCTION", production)

    result = deploy.rollback_drill(candidate)

    assert result["ok"] is True
    assert result["rollback_source"] == "previous"
    assert result["restored"] == str(previous.resolve())


def test_rollback_integrity_does_not_run_candidate_runtime_gates(
    tmp_path,
    monkeypatch,
) -> None:
    production = tmp_path / "production"
    release = _release(production, "a" * 40, "a" * 40)
    monkeypatch.setattr(deploy, "PRODUCTION", production)
    monkeypatch.setattr(
        deploy,
        "gate_marker_details",
        lambda path: pytest.fail("rollback integrity must not evaluate gates"),
    )
    monkeypatch.setattr(
        deploy,
        "port_free",
        lambda port: pytest.fail("rollback integrity must not inspect runtime"),
    )

    result = deploy.rollback_integrity(release)

    assert result == {
        "release_under_root": True,
        "release_metadata": True,
        "release_identity": True,
        "manifest": True,
        "allowed": True,
    }


def test_main_exits_nonzero_when_publish_cannot_preserve_rollback(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(deploy, "preflight", lambda release: {"allowed": True})
    monkeypatch.setattr(
        deploy,
        "publish",
        lambda release: {"published": False},
    )
    monkeypatch.setattr(
        "sys.argv",
        ["deploy_local_arch_release.py", str(tmp_path), "--publish"],
    )

    assert deploy.main() == 3
