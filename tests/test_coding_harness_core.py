from pathlib import Path
import subprocess

from ralfloop_agent.coding_harness.harness import (
    HarnessConfig,
    SnapshotEntry,
    assess_coding_risk,
    build_diff,
    changed_paths,
    protected_changes,
    run_harness,
    _pi_effective_return_code,
    _run_shell,
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


def test_pi_json_api_error_is_nonzero_even_when_process_exit_is_zero():
    output = "\n".join((
        '{"type":"message_end","message":{"role":"assistant","stopReason":"error","errorMessage":"Connection error."}}',
        '{"type":"auto_retry_end","success":false,"attempt":3,"finalError":"Connection error."}',
        '{"type":"agent_settled"}',
    ))
    assert _pi_effective_return_code(0, output) == 70


def test_pi_successful_retry_remains_success():
    output = "\n".join((
        '{"type":"message_end","message":{"role":"assistant","stopReason":"error"}}',
        '{"type":"auto_retry_end","success":true,"attempt":1}',
        '{"type":"message_end","message":{"role":"assistant","stopReason":"stop"}}',
    ))
    assert _pi_effective_return_code(0, output) == 0


def test_dead_worker_without_patch_skips_semantic_judge(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        "ralfloop_agent.coding_harness.harness._run_worker",
        lambda config, prompt: (70, "provider unavailable"),
    )
    monkeypatch.setattr(
        "ralfloop_agent.coding_harness.harness._run_shell",
        lambda command, root, timeout_sec: (0, "ok"),
    )

    class JudgeMustNotRun:
        def review(self, packet):
            raise AssertionError("semantic judge should not run for a dead worker with no patch")

    report = run_harness(
        HarnessConfig(
            workdir=tmp_path,
            task="fix one small bug",
            validator_command="git diff --check",
        ),
        judge=JudgeMustNotRun(),
    )

    assert report["final_status"] == "fail_closed"
    assert report["decision"] == "worker_failed_without_changes"
    assert report["ds4_invoked"] is False
    assert report["changed_files"] == []


def test_default_git_diff_validator_does_not_require_shell_parser(tmp_path: Path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "validator-test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Validator Test"], check=True)
    target = tmp_path / "calc.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "calc.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-q", "-m", "base"], check=True)
    target.write_text("VALUE = 2\n", encoding="utf-8")

    rc, output = _run_shell("git diff --check", tmp_path, 10)

    assert rc == 0
    assert output == ""
