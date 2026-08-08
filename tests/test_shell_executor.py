from pathlib import Path
import os
import time

import pytest

from src.executor import ShellExecutor, cleanup_old_sandboxes


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


def test_task_id_creates_isolated_sandbox(tmp_path: Path):
    executor = ShellExecutor(base_dir=tmp_path, task_id="task-123")

    evidence = executor.run_in_sandbox(["pwd"])

    assert evidence.exit_code == 0
    assert evidence.path == str((tmp_path / "task-123").resolve())
    assert Path(evidence.path).exists()


def test_task_id_is_sanitized(tmp_path: Path):
    executor = ShellExecutor(base_dir=tmp_path, task_id="../bad/id")

    assert executor.base_dir == (tmp_path / "bad_id").resolve()


def test_timeout(tmp_path: Path):
    executor = ShellExecutor(base_dir=tmp_path, timeout_sec=1)

    evidence = executor.run_in_sandbox(["sleep", "60"])

    assert evidence.exit_code == -1
    assert "Timeout after 1s" in (evidence.stderr or "")


def test_cleanup_old_sandboxes(tmp_path: Path):
    old = tmp_path / "old-task"
    fresh = tmp_path / "fresh-task"
    old.mkdir()
    fresh.mkdir()
    old_time = time.time() - (3 * 3600)
    os.utime(old, (old_time, old_time))

    removed = cleanup_old_sandboxes(max_age_hours=1, base_dir=tmp_path)

    assert old in removed
    assert not old.exists()
    assert fresh.exists()
