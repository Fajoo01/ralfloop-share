from pathlib import Path

import pytest

from src.executor import ShellExecutor


def test_run_simple_command(tmp_path: Path):
    executor = ShellExecutor(base_dir=tmp_path)

    evidence = executor.run_in_sandbox(["echo", "hello"])

    assert evidence.command == "echo hello"
    assert evidence.path == str(tmp_path.resolve())
    assert evidence.exit_code == 0
    assert evidence.stdout == "hello\n"


def test_run_failing_command(tmp_path: Path):
    executor = ShellExecutor(base_dir=tmp_path)

    evidence = executor.run_in_sandbox(["false"])

    assert evidence.exit_code != 0
    assert evidence.command == "false"


def test_run_outside_sandbox(tmp_path: Path):
    executor = ShellExecutor(base_dir=tmp_path)

    with pytest.raises(ValueError):
        executor.run_in_sandbox(["pwd"], cwd="/etc")


def test_timeout(tmp_path: Path):
    executor = ShellExecutor(base_dir=tmp_path, timeout_sec=1)

    evidence = executor.run_in_sandbox(["sleep", "60"])

    assert evidence.exit_code == -1
    assert "Timeout after 1s" in (evidence.stderr or "")
