from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ralfloop_agent.cli import repo_context


def test_repo_context_is_bounded_and_reads_only_known_files(tmp_path):
    (tmp_path / "README.md").write_text("x" * 5000, encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("bounded instructions", encoding="utf-8")
    (tmp_path / ".env").write_text("PASSWORD=never-read", encoding="utf-8")
    (tmp_path / "secret-notes.txt").write_text("never-read", encoding="utf-8")

    context = repo_context.collect_repo_context(tmp_path, max_bytes=128, max_files=2)
    serialized = repr(context)

    assert context["cwd"] == str(tmp_path.resolve())
    assert context["repository"] == tmp_path.name
    assert context["bytes"] <= 128
    assert context["truncated"] is True
    assert "PASSWORD" not in serialized
    assert "never-read" not in serialized


def test_repo_context_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("outside-secret", encoding="utf-8")
    os.symlink(outside, tmp_path / "README.md")

    context = repo_context.collect_repo_context(tmp_path)

    assert context["files"] == []
    assert "outside-secret" not in repr(context)


def test_repo_context_ignores_git_metadata_symlink_escape(tmp_path, monkeypatch):
    outside = tmp_path.parent / f"{tmp_path.name}-git"
    outside.mkdir()
    os.symlink(outside, tmp_path / ".git")
    monkeypatch.setattr(
        repo_context,
        "_git",
        lambda *args, **kwargs: pytest.fail("git must not run for escaped metadata"),
    )

    context = repo_context.collect_repo_context(tmp_path)

    assert context["branch"] is None
    assert context["git_status"] == []


def test_repo_context_serialized_payload_has_hard_limit(tmp_path):
    (tmp_path / "README.md").write_bytes(b"\x00" * repo_context.DEFAULT_MAX_BYTES)

    context = repo_context.collect_repo_context(tmp_path)

    serialized = json.dumps(context, ensure_ascii=False, sort_keys=True).encode("utf-8")
    assert len(serialized) <= repo_context.MAX_REPO_CONTEXT_BYTES
    assert context["truncated"] is True


def test_repo_context_filters_sensitive_git_rows():
    rows = repo_context._safe_git_lines(" M src/app.py\n M .env\n?? api_token.txt", 10)
    assert rows == [" M src/app.py"]


def test_repo_context_exposes_bounded_git_metadata(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()

    def fake_git(root, *args):
        if args[0] == "rev-parse":
            return "feature/chat"
        if args[0] == "status":
            return " M app.py"
        return "abc123 First\ndef456 Second"

    monkeypatch.setattr(repo_context, "_git", fake_git)
    context = repo_context.collect_repo_context(tmp_path, max_commits=1)

    assert context["branch"] == "feature/chat"
    assert context["git_status"] == [" M app.py"]
    assert context["recent_commits"] == ["abc123 First"]


def test_git_metadata_commands_disable_locks_fsmonitor_and_external_config(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return ""

    monkeypatch.setattr(repo_context, "_run_bounded", fake_run)
    monkeypatch.setenv("GIT_DIR", "/outside/.git")
    monkeypatch.setenv("GIT_WORK_TREE", "/outside")

    repo_context.collect_repo_context(tmp_path)

    assert calls
    for command, kwargs in calls:
        assert ["-c", "core.fsmonitor=false"] == command[3:5]
        assert "--untracked-files=normal" in command or "status" not in command
        assert "--ignore-submodules=all" in command or "status" not in command
        assert "core.excludesFile=/dev/null" in command
        assert "core.attributesFile=/dev/null" in command
        assert kwargs["env"]["GIT_OPTIONAL_LOCKS"] == "0"
        assert kwargs["env"]["GIT_CONFIG_NOSYSTEM"] == "1"
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert "GIT_DIR" not in kwargs["env"]
        assert "GIT_WORK_TREE" not in kwargs["env"]


def test_git_environment_cannot_redirect_collector_outside_cwd(monkeypatch, tmp_path):
    if repo_context.shutil.which("git", path=os.defpath) is None:
        pytest.skip("git unavailable")
    inside = tmp_path / "inside"
    outside = tmp_path / "outside"
    inside.mkdir()
    outside.mkdir()
    subprocess.run(["git", "init", "-q", str(inside)], check=True)
    subprocess.run(["git", "init", "-q", str(outside)], check=True)
    (inside / "inside-only.txt").write_text("inside", encoding="utf-8")
    (inside / "node_modules").mkdir()
    (inside / "node_modules" / "excluded.txt").write_text("excluded", encoding="utf-8")
    (outside / "outside-only.txt").write_text("outside", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(outside / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(outside))

    context = repo_context.collect_repo_context(inside)

    assert any("inside-only.txt" in row for row in context["git_status"])
    assert all("outside-only.txt" not in row for row in context["git_status"])
    assert all("node_modules" not in row for row in context["git_status"])


def test_git_external_excludes_file_cannot_hide_cwd_status(tmp_path):
    if repo_context.shutil.which("git", path=os.defpath) is None:
        pytest.skip("git unavailable")
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    outside_excludes = tmp_path / "outside-excludes"
    outside_excludes.write_text("inside-only.txt\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(root), "config", "core.excludesFile", str(outside_excludes)],
        check=True,
    )
    (root / "inside-only.txt").write_text("inside", encoding="utf-8")

    context = repo_context.collect_repo_context(root)

    assert any("inside-only.txt" in row for row in context["git_status"])


def test_git_process_output_is_bounded():
    output = repo_context._run_bounded(
        [sys.executable, "-c", "import sys; sys.stdout.write('x' * 10000)"],
        env={"PATH": os.defpath},
        timeout=2,
        max_bytes=128,
    )

    assert len(output.encode("utf-8")) <= 128


def test_git_metadata_with_include_is_rejected_before_git(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text('[include]\npath = "/outside/config"\n', encoding="utf-8")
    monkeypatch.setattr(
        repo_context,
        "_git",
        lambda *args, **kwargs: pytest.fail("git must not run with external include"),
    )

    context = repo_context.collect_repo_context(tmp_path)

    assert context["branch"] is None


def test_local_branch_survives_external_object_store_without_reading_it(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    (git_dir / "objects" / "info").mkdir(parents=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/feature/local\n", encoding="ascii")
    (git_dir / "objects" / "info" / "alternates").write_text("/outside/objects\n", encoding="utf-8")
    monkeypatch.setattr(
        repo_context,
        "_git",
        lambda *args, **kwargs: pytest.fail("git must not run with external object store"),
    )

    context = repo_context.collect_repo_context(tmp_path)

    assert context["branch"] == "feature/local"
    assert context["git_metadata_limited"] is True


def test_git_metadata_with_external_worktree_is_rejected(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text('[core]\nworktree = "/outside"\n', encoding="utf-8")
    monkeypatch.setattr(
        repo_context,
        "_git",
        lambda *args, **kwargs: pytest.fail("git must not run with external worktree"),
    )

    context = repo_context.collect_repo_context(tmp_path)

    assert context["git_status"] == []


def test_git_metadata_with_worktree_config_is_rejected(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text("[extensions]\nworktreeConfig = true\n", encoding="utf-8")
    (git_dir / "config.worktree").write_text('[core]\nworktree = "/outside"\n', encoding="utf-8")
    monkeypatch.setattr(
        repo_context,
        "_git",
        lambda *args, **kwargs: pytest.fail("git must not run with worktree config"),
    )

    context = repo_context.collect_repo_context(tmp_path)

    assert context["branch"] is None


@pytest.mark.parametrize("value", ["missing", "file.txt"])
def test_repo_context_rejects_invalid_cwd(tmp_path, value):
    path = tmp_path / value
    if value.endswith(".txt"):
        path.write_text("x", encoding="utf-8")
    with pytest.raises(repo_context.RepoContextError, match="invalid_cwd"):
        repo_context.collect_repo_context(path)
