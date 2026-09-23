from __future__ import annotations

from pathlib import Path
import subprocess

from ralfloop_agent.programmer import ProgrammerAgent, ProgrammerConfig, ProgrammerState


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main"
    worktree = tmp_path / "ticket"
    main.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(main)], check=True)
    _git(main, "config", "user.email", "programmer-test@example.invalid")
    _git(main, "config", "user.name", "Programmer Test")
    (main / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(main, "add", "app.py")
    _git(main, "commit", "-q", "-m", "base")
    _git(main, "worktree", "add", "-q", "-b", "ticket", str(worktree))
    return main, worktree


def test_programmer_builds_candidate_without_changing_head(tmp_path: Path) -> None:
    _, worktree = _linked_worktree(tmp_path)
    state_root = tmp_path / "state"
    captured = {}

    def runner(config):
        captured["tools"] = config.worker_tools
        (config.workdir / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
        return {
            "final_status": "pass",
            "decision": "deterministic_fast_path",
            "changed_files": ["app.py"],
        }

    config = ProgrammerConfig(
        workdir=worktree,
        task="change VALUE to 2",
        allowed_roots=(tmp_path,),
        state_root=state_root,
    )
    before = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    result = ProgrammerAgent(config, runner=runner).run()
    after = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    assert result.state is ProgrammerState.CANDIDATE_READY
    assert result.reason == "deterministic_fast_path"
    assert before == after == result.base_head == result.final_head
    assert captured["tools"] == ("read", "edit", "write")
    assert (worktree / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (state_root / "runs" / f"{result.run_id}.json").is_file()


def test_programmer_blocks_dirty_worktree_before_worker(tmp_path: Path) -> None:
    _, worktree = _linked_worktree(tmp_path)
    (worktree / "notes.txt").write_text("human work\n", encoding="utf-8")
    called = False

    def runner(config):
        nonlocal called
        called = True
        return {"final_status": "pass"}

    result = ProgrammerAgent(
        ProgrammerConfig(
            workdir=worktree,
            task="edit app.py",
            allowed_roots=(tmp_path,),
            state_root=tmp_path / "state",
        ),
        runner=runner,
    ).run()

    assert result.state is ProgrammerState.BLOCKED
    assert result.reason == "worktree_dirty"
    assert called is False
    assert (worktree / "notes.txt").read_text(encoding="utf-8") == "human work\n"


def test_programmer_fails_closed_if_worker_changes_git_head(tmp_path: Path) -> None:
    _, worktree = _linked_worktree(tmp_path)

    def runner(config):
        (config.workdir / "app.py").write_text("VALUE = 3\n", encoding="utf-8")
        _git(config.workdir, "add", "app.py")
        _git(config.workdir, "commit", "-q", "-m", "worker must not commit")
        return {"final_status": "pass", "decision": "unexpected_commit"}

    result = ProgrammerAgent(
        ProgrammerConfig(
            workdir=worktree,
            task="edit app.py",
            allowed_roots=(tmp_path,),
            state_root=tmp_path / "state",
        ),
        runner=runner,
    ).run()

    assert result.state is ProgrammerState.FAIL_CLOSED
    assert result.reason == "worker_changed_head"
    assert result.final_head != result.base_head


def test_programmer_requires_explicit_allowed_roots(tmp_path: Path) -> None:
    _, worktree = _linked_worktree(tmp_path)
    result = ProgrammerAgent(
        ProgrammerConfig(
            workdir=worktree,
            task="edit app.py",
            state_root=tmp_path / "state",
        ),
        runner=lambda config: {"final_status": "pass"},
    ).run()
    assert result.state is ProgrammerState.BLOCKED
    assert result.reason == "allowed_roots_missing"
