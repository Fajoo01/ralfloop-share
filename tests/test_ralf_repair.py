from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

from ralfloop_agent.cli import terminal_chat
from ralfloop_agent.repair import PatchValidator, RepairManager


def _run(argv, cwd: Path):
    return subprocess.run(
        argv,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def _synthetic_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "router.py").write_text(
        'TRIGGERS = ["posta"]\n\n'
        "def matches(goal):\n"
        "    return any(trigger in goal for trigger in TRIGGERS)\n",
        encoding="utf-8",
    )
    (repo / "tests" / "test_router.py").write_text(
        "from router import matches\n\n"
        "def test_posta_not_inside_spostare():\n"
        "    assert matches('spostare') is False\n",
        encoding="utf-8",
    )
    assert _run(["git", "init", "-q"], repo).returncode == 0
    assert _run(["git", "config", "user.email", "fixture@example.invalid"], repo).returncode == 0
    assert _run(["git", "config", "user.name", "Fixture"], repo).returncode == 0
    assert _run(["git", "add", "--", "router.py", "tests/test_router.py"], repo).returncode == 0
    assert _run(["git", "commit", "-qm", "fixture"], repo).returncode == 0
    return repo


PATCH = """diff --git a/router.py b/router.py
--- a/router.py
+++ b/router.py
@@ -1,4 +1,6 @@
+import re
+
 TRIGGERS = ["posta"]
-
-def matches(goal):
-    return any(trigger in goal for trigger in TRIGGERS)
+
+def matches(goal):
+    return any(re.search(rf"(?<!\\w){re.escape(trigger)}(?!\\w)", goal) for trigger in TRIGGERS)
"""


def test_patch_validator_blocks_limits_paths_symlinks_and_bypass(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    validator = PatchValidator(max_files=1, max_changed_lines=20, allowed_paths=("router.py",))
    assert validator.validate(PATCH, repo).ok is True
    forbidden = PATCH.replace("router.py", "main_plugin.py")
    assert "forbidden_file:main_plugin.py" in validator.validate(forbidden, repo).errors
    bypass = PATCH.replace("import re", "human_confirmed=True")
    assert "approval_bypass_forbidden" in validator.validate(bypass, repo).errors
    too_many = PATCH + PATCH.replace("router.py", "other.py")
    assert "max_files_exceeded" in validator.validate(too_many, repo).errors
    (repo / "link.py").symlink_to(repo / "router.py")
    symlink_patch = PATCH.replace("router.py", "link.py")
    assert "symlink_target_forbidden:link.py" in validator.validate(symlink_patch, repo).errors


def test_repair_plan_creates_no_worktree(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    manager = RepairManager(
        repo,
        state_root=tmp_path / "state",
        code_retriever=lambda *args: {"ok": False, "files": []},
        proposer=lambda *args: "",
    )
    record = manager.plan("fix matcher")
    assert record.status == "planned"
    assert record.worktree is None
    assert not (tmp_path / "state" / "runs").exists()


def test_self_repair_canary_isolated_and_approval_pending(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    before_head = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    assert _run([sys.executable, "-m", "pytest", "-q"], repo).returncode != 0

    def retrieve(worktree, description):
        return {
            "ok": True,
            "files": ["router.py"],
            "result_envelope": {"ok": True, "tool_id": "code_retriever_fixture"},
        }

    manager = RepairManager(
        repo,
        state_root=tmp_path / "state",
        python_executable=sys.executable,
        code_retriever=retrieve,
        proposer=lambda worktree, description, files: PATCH,
    )
    record = manager.run("posta must not match spostare")

    assert record.status == "approval_pending"
    assert record.approval_required is True
    assert record.approval_status == "pending_user"
    assert record.selected_files == ["router.py"]
    assert record.pre_tests[0]["exit_code"] != 0
    assert all(row["exit_code"] == 0 for row in record.post_tests)
    assert "+    return any(re.search" in record.diff
    assert Path(record.worktree).is_dir()
    assert "trigger in goal" not in (Path(record.worktree) / "router.py").read_text(encoding="utf-8")
    assert "return any(trigger in goal" in (repo / "router.py").read_text(encoding="utf-8")
    assert _run(["git", "rev-parse", "HEAD"], repo).stdout.strip() == before_head
    assert _run(["git", "status", "--porcelain"], repo).stdout == ""
    assert _run(["git", "status", "--porcelain"], Path(record.worktree)).stdout.strip() == "M router.py"
    assert any("worktree remove" in row for row in record.rollback)
    assert manager.status(record.run_id).status == "approval_pending"


def test_repair_cli_plan_and_status_with_injected_manager(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    manager = RepairManager(
        repo,
        state_root=tmp_path / "state",
        code_retriever=lambda *args: {"ok": False, "files": []},
        proposer=lambda *args: "",
    )
    plan_args = terminal_chat.build_parser().parse_args(["repair", "plan", "matcher bug"])
    out = io.StringIO()
    assert terminal_chat.run_repair_command(plan_args, manager=manager, out=out, err=io.StringIO()) == 0
    run_id = json.loads(out.getvalue())["run_id"]
    status_args = terminal_chat.build_parser().parse_args(["repair", "status", run_id])
    status_out = io.StringIO()
    assert terminal_chat.run_repair_command(status_args, manager=manager, out=status_out, err=io.StringIO()) == 0
    assert json.loads(status_out.getvalue())["status"] == "planned"
