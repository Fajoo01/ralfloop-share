from pathlib import Path

from ralfloop_agent.coding_harness.harness import (
    HarnessConfig,
    SnapshotEntry,
    assess_coding_risk,
    build_diff,
    changed_paths,
    protected_changes,
    _run_worker,
)


def test_small_green_change_is_low_risk(tmp_path: Path):
    risk = assess_coding_risk(
        worker_rc=0,
        validator_rc=0,
        changed_files=["calc.py"],
        changed_lines=2,
        protected=[],
        diff_truncated=False,
    )
    assert risk.level == "low"


def test_worker_timeout_is_high_risk_even_if_tests_green():
    risk = assess_coding_risk(
        worker_rc=124,
        validator_rc=0,
        changed_files=["calc.py"],
        changed_lines=2,
        protected=[],
        diff_truncated=False,
    )
    assert risk.level == "high"
    assert "worker_nonzero:124" in risk.reasons


def test_validator_red_is_high_risk():
    risk = assess_coding_risk(
        worker_rc=0,
        validator_rc=1,
        changed_files=["calc.py"],
        changed_lines=2,
        protected=[],
        diff_truncated=False,
    )
    assert risk.level == "high"


def test_tests_are_protected_by_default(tmp_path: Path):
    config = HarnessConfig(
        workdir=tmp_path,
        task="x",
        validator_command="true",
    )
    assert protected_changes(
        config,
        ["calc.py", "tests/test_calc.py"],
    ) == ["tests/test_calc.py"]


def test_snapshot_delta_and_diff():
    before = {
        "calc.py": SnapshotEntry("a", 10, "def add(a,b):\n    return a-b\n")
    }
    after = {
        "calc.py": SnapshotEntry("b", 10, "def add(a,b):\n    return a+b\n")
    }
    changed = changed_paths(before, after)
    diff, lines, truncated = build_diff(before, after, changed)

    assert changed == ["calc.py"]
    assert "return a-b" in diff
    assert "return a+b" in diff
    assert lines == 2
    assert truncated is False


def test_worker_uses_configured_fallback_after_primary_failure(monkeypatch, tmp_path: Path):
    calls = []

    def fake_once(config, prompt, *, provider, model):
        calls.append((provider, model))
        if provider == "broken-local":
            return 124, "timeout"
        return 0, "done"

    monkeypatch.setattr(
        "ralfloop_agent.coding_harness.harness._run_worker_once", fake_once
    )
    config = HarnessConfig(
        workdir=tmp_path,
        task="x",
        validator_command="true",
        provider="broken-local",
        model="broken-model",
        fallback_provider="llamacpp-code-local",
        fallback_model="qwen2.5-coder-7b",
    )
    rc, output = _run_worker(config, "prompt")
    assert rc == 0
    assert calls == [
        ("broken-local", "broken-model"),
        ("llamacpp-code-local", "qwen2.5-coder-7b"),
    ]
    assert "PRIMARY_WORKER_FAILED" in output
    assert "FALLBACK_WORKER" in output
