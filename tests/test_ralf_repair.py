from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

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


@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_repair_run_blocks_dirty_source_before_worktree_and_models(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    repo = _synthetic_repo(tmp_path)

    if dirty_kind == "tracked":
        (repo / "router.py").write_text(
            (repo / "router.py").read_text(encoding="utf-8")
            + "\n# local change\n",
            encoding="utf-8",
        )
    else:
        (repo / "untracked.py").write_text(
            "value = 1\n",
            encoding="utf-8",
        )

    calls: list[str] = []
    manager = RepairManager(
        repo,
        state_root=tmp_path / "state",
        code_retriever=lambda *args: calls.append("retrieve") or {},
        proposer=lambda *args: calls.append("propose") or PATCH,
    )

    record = manager.run("fix matcher")

    assert record.status == "blocked"
    assert record.error == "repair_source_dirty"
    assert record.worktree is None
    assert record.approval_required is False
    assert record.validation["source_preflight"]["clean"] is False
    assert calls == []
    assert not (tmp_path / "state" / "runs").exists()
    assert manager.status(record.run_id).error == "repair_source_dirty"


def test_self_repair_canary_isolated_and_approval_pending(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    before_head = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    assert _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        repo,
    ).returncode != 0

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
    saved_statuses: list[str] = []
    original_save = manager.store.save

    def recording_save(record):
        saved_statuses.append(record.status)
        original_save(record)

    manager.store.save = recording_save
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
    assert saved_statuses == [
        "source_preflight",
        "creating_worktree",
        "diagnosing",
        "proposing",
        "verifying",
        "approval_pending",
    ]
    assert (
        record.validation["source_preflight"]["head"]["stdout"].strip()
        == before_head
    )


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



def test_patch_proposer_does_not_post_before_gpu_session(tmp_path):
    from contextlib import contextmanager
    import pytest

    from ralfloop_agent.repair.workflow import (
        LlamaCppPatchProposer,
    )

    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")

    class FailingScheduler:
        @contextmanager
        def engine_session(self, engine, *, task_id=""):
            assert engine == "qwen_chat"
            raise RuntimeError("gpu_session_failed")
            yield {}

    class Session:
        called = False

        def post(self, *args, **kwargs):
            self.called = True
            raise AssertionError("POST must not be called")

    session = Session()

    proposer = LlamaCppPatchProposer(
        session=session,
        scheduler=FailingScheduler(),
    )

    with pytest.raises(
        RuntimeError,
        match="gpu_session_failed",
    ):
        proposer(
            tmp_path,
            "test",
            ["sample.py"],
        )

    assert session.called is False


def test_edit_protocol_v2_builds_git_valid_diff(tmp_path):
    from contextlib import contextmanager
    import subprocess

    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    source = tmp_path / "sample.py"
    source.write_text(
        "value = 1\nprint(value)\n",
        encoding="utf-8",
    )

    class Scheduler:
        entered = False

        @contextmanager
        def engine_session(self, engine, *, task_id=""):
            assert engine == "qwen_chat"
            self.entered = True
            yield {"gpu_lock_owner": "test"}

    class Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "@@RALF_EDIT path=sample.py start=1 end=2\n"
                                "value = 2\n"
                                "@@RALF_END\n"
                            )
                        }
                    }
                ]
            }

        def close(self):
            return None

    class Session:
        payload = None

        def post(self, url, **kwargs):
            self.payload = kwargs["json"]
            return Response()

    scheduler = Scheduler()
    session = Session()

    proposer = LlamaCppPatchProposer(
        session=session,
        scheduler=scheduler,
    )

    patch = proposer(
        tmp_path,
        "Change value from 1 to 2",
        ["sample.py"],
    )

    assert scheduler.entered is True
    assert "response_format" not in session.payload
    assert "000001|value = 1" in (
        session.payload["messages"][1]["content"]
    )
    assert "-value = 1" in patch
    assert "+value = 2" in patch

    check = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=tmp_path,
        input=patch,
        text=True,
        capture_output=True,
        check=False,
    )

    assert check.returncode == 0, check.stderr


def test_edit_protocol_v2_repeated_text_is_not_ambiguous():
    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    modified = LlamaCppPatchProposer._apply_plan(
        {
            "sample.py": (
                "value = 1\n"
                "value = 1\n"
            )
        },
        ["sample.py"],
        {
            "edits": [
                {
                    "path": "sample.py",
                    "start": 2,
                    "end": 3,
                    "replacement": "value = 2\n",
                }
            ]
        },
    )

    assert modified["sample.py"] == (
        "value = 1\n"
        "value = 2\n"
    )


def test_edit_protocol_v2_supports_safe_append():
    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    modified = LlamaCppPatchProposer._apply_plan(
        {"sample.py": "value = 1\n"},
        ["sample.py"],
        {
            "edits": [
                {
                    "path": "sample.py",
                    "start": 2,
                    "end": 2,
                    "replacement": (
                        "\ndef test_marker():\n"
                        "    assert True\n"
                    ),
                }
            ]
        },
    )

    assert modified["sample.py"] == (
        "value = 1\n"
        "\n"
        "def test_marker():\n"
        "    assert True\n"
    )


def test_edit_protocol_v2_rejects_unselected_path():
    import pytest

    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    with pytest.raises(
        RuntimeError,
        match="repair_structured_path_forbidden",
    ):
        LlamaCppPatchProposer._apply_plan(
            {"sample.py": "x = 1\n"},
            ["sample.py"],
            {
                "edits": [
                    {
                        "path": "../../evil.py",
                        "start": 1,
                        "end": 2,
                        "replacement": "x = 2\n",
                    }
                ]
            },
        )


def test_edit_protocol_v2_rejects_out_of_bounds():
    import pytest

    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    with pytest.raises(
        RuntimeError,
        match="repair_edit_range_bounds",
    ):
        LlamaCppPatchProposer._apply_plan(
            {"sample.py": "x = 1\n"},
            ["sample.py"],
            {
                "edits": [
                    {
                        "path": "sample.py",
                        "start": 3,
                        "end": 3,
                        "replacement": "x = 2\n",
                    }
                ]
            },
        )


def test_edit_protocol_v2_rejects_overlap():
    import pytest

    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    with pytest.raises(
        RuntimeError,
        match="repair_edit_overlap",
    ):
        LlamaCppPatchProposer._apply_plan(
            {
                "sample.py": (
                    "a = 1\n"
                    "b = 2\n"
                    "c = 3\n"
                )
            },
            ["sample.py"],
            {
                "edits": [
                    {
                        "path": "sample.py",
                        "start": 1,
                        "end": 3,
                        "replacement": "a = 10\n",
                    },
                    {
                        "path": "sample.py",
                        "start": 2,
                        "end": 4,
                        "replacement": "b = 20\n",
                    },
                ]
            },
        )


def test_edit_protocol_v2_rejects_non_visible_range():
    import pytest

    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    with pytest.raises(
        RuntimeError,
        match="repair_edit_range_not_visible",
    ):
        LlamaCppPatchProposer._apply_plan(
            {
                "sample.py": (
                    "a = 1\n"
                    "b = 2\n"
                    "c = 3\n"
                )
            },
            ["sample.py"],
            {
                "edits": [
                    {
                        "path": "sample.py",
                        "start": 3,
                        "end": 4,
                        "replacement": "c = 30\n",
                    }
                ]
            },
            visible_spans={"sample.py": [(1, 2)]},
        )



def test_edit_protocol_v2_accepts_double_quoted_header_values():
    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    edits = LlamaCppPatchProposer._parse_edit_protocol(
        (
            '@@RALF_EDIT path="sample.py" start="1" end="2"\n'
            'x = 2\n'
            '@@RALF_END\n'
        ),
        ["sample.py"],
    )

    assert edits == [
        {
            "path": "sample.py",
            "start": 1,
            "end": 2,
            "replacement": "x = 2\n",
        }
    ]


def test_edit_protocol_v2_requires_end_marker():
    import pytest

    from ralfloop_agent.repair.workflow import LlamaCppPatchProposer

    with pytest.raises(
        RuntimeError,
        match="repair_edit_missing_end",
    ):
        LlamaCppPatchProposer._parse_edit_protocol(
            (
                "@@RALF_EDIT path=sample.py start=1 end=2\n"
                "x = 2\n"
            ),
            ["sample.py"],
        )



def test_verification_failure_gets_one_bounded_retry(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    original = (repo / "router.py").read_text(encoding="utf-8")

    invalid_patch = """diff --git a/router.py b/router.py
--- a/router.py
+++ b/router.py
@@ -2,3 +2,3 @@

 def matches(goal):
-    return any(trigger in goal for trigger in TRIGGERS)
+    return (
"""

    calls: list[str] = []

    def proposer(worktree, description, files):
        calls.append(description)
        if len(calls) == 1:
            return invalid_patch
        if len(calls) == 2:
            return PATCH
        raise AssertionError("third proposer call forbidden")

    def retrieve(worktree, description):
        return {
            "ok": True,
            "files": ["router.py"],
            "result_envelope": {
                "ok": True,
                "tool_id": "fixture",
            },
        }

    manager = RepairManager(
        repo,
        state_root=tmp_path / "state",
        python_executable=sys.executable,
        code_retriever=retrieve,
        proposer=proposer,
    )

    record = manager.run("fix matcher")

    assert len(calls) == 2
    assert "CORRECTION ATTEMPT 2 OF 2" in calls[1]
    assert "py_compile" in calls[1]
    assert record.status == "approval_pending"
    assert record.approval_required is True
    assert record.approval_status == "pending_user"
    assert record.error is None
    assert record.validation["attempts"][0]["ok"] is False
    assert record.validation["attempts"][1]["ok"] is True
    assert (repo / "router.py").read_text(encoding="utf-8") == original
    assert "import re" in (
        Path(record.worktree) / "router.py"
    ).read_text(encoding="utf-8")


def test_verification_retry_stops_after_two_failures(tmp_path: Path) -> None:
    repo = _synthetic_repo(tmp_path)
    original = (repo / "router.py").read_text(encoding="utf-8")

    invalid_patch = """diff --git a/router.py b/router.py
--- a/router.py
+++ b/router.py
@@ -2,3 +2,3 @@

 def matches(goal):
-    return any(trigger in goal for trigger in TRIGGERS)
+    return (
"""

    calls: list[str] = []

    def proposer(worktree, description, files):
        calls.append(description)
        if len(calls) > 2:
            raise AssertionError("third proposer call forbidden")
        return invalid_patch

    def retrieve(worktree, description):
        return {
            "ok": True,
            "files": ["router.py"],
            "result_envelope": {
                "ok": True,
                "tool_id": "fixture",
            },
        }

    manager = RepairManager(
        repo,
        state_root=tmp_path / "state",
        python_executable=sys.executable,
        code_retriever=retrieve,
        proposer=proposer,
    )

    record = manager.run("fix matcher")

    assert len(calls) == 2
    assert record.status == "failed"
    assert record.error == "deterministic_verification_failed"
    assert record.approval_required is False
    assert len(record.validation["attempts"]) == 2
    assert (repo / "router.py").read_text(encoding="utf-8") == original



def test_repair_cli_without_manager_uses_backend_only(
    monkeypatch,
) -> None:
    calls = []

    def fake_request(
        method,
        path,
        *,
        payload=None,
        timeout=30.0,
    ):
        calls.append(
            (method, path, payload, timeout)
        )

        if path == "/repairs/plan":
            return {
                "run_id": "a" * 32,
                "status": "planned",
            }

        if path == "/repairs/" + ("a" * 32):
            return {
                "run_id": "a" * 32,
                "status": "planned",
            }

        raise AssertionError(path)

    monkeypatch.setattr(
        terminal_chat,
        "_repair_backend_request",
        fake_request,
    )

    plan_args = (
        terminal_chat.build_parser().parse_args(
            ["repair", "plan", "matcher bug"]
        )
    )

    out = io.StringIO()

    assert terminal_chat.run_repair_command(
        plan_args,
        out=out,
        err=io.StringIO(),
    ) == 0

    assert calls[0][0] == "POST"
    assert calls[0][1] == "/repairs/plan"
    assert calls[0][2]["description"] == "matcher bug"

    status_args = (
        terminal_chat.build_parser().parse_args(
            ["repair", "status", "a" * 32]
        )
    )

    out = io.StringIO()

    assert terminal_chat.run_repair_command(
        status_args,
        out=out,
        err=io.StringIO(),
    ) == 0

    assert calls[1][0] == "GET"
    assert calls[1][1] == "/repairs/" + ("a" * 32)


def test_repair_cli_approval_and_apply_use_backend(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    calls = []

    def fake_request(
        method,
        path,
        *,
        payload=None,
        timeout=30.0,
    ):
        calls.append((method, path, payload))

        if path.endswith("/approval-request"):
            return {
                "status": "approval_requested",
                "request_id": "apr_test",
            }

        if path == "/repair-approvals/apr_test/apply":
            return {
                "status": "executed",
            }

        raise AssertionError(path)

    monkeypatch.setattr(
        terminal_chat,
        "_repair_backend_request",
        fake_request,
    )

    request_args = SimpleNamespace(
        repair_action="request-approval",
        run_id="b" * 32,
    )

    assert terminal_chat.run_repair_command(
        request_args,
        out=io.StringIO(),
        err=io.StringIO(),
    ) == 0

    assert calls[0][0] == "POST"
    assert calls[0][1] == (
        "/repairs/" + ("b" * 32) + "/approval-request"
    )

    apply_args = SimpleNamespace(
        repair_action="apply",
        request_id="apr_test",
    )

    assert terminal_chat.run_repair_command(
        apply_args,
        out=io.StringIO(),
        err=io.StringIO(),
    ) == 0

    assert calls[1][1] == (
        "/repair-approvals/apr_test/apply"
    )


def test_repair_repo_policy_uses_exact_resolved_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from ralfloop_agent.repair.approval import (
        _repair_repo_policy,
        _select_repair_repo,
    )

    canonical = tmp_path / "canonical"
    allowed_extra = tmp_path / "allowed-extra"
    forbidden = tmp_path / "forbidden"
    prefix_trick = tmp_path / "allowed-extra-evil"
    for path in (
        canonical,
        allowed_extra,
        forbidden,
        prefix_trick,
    ):
        path.mkdir()

    allowed_alias = tmp_path / "allowed-alias"
    allowed_alias.symlink_to(allowed_extra, target_is_directory=True)
    escape = tmp_path / "allowed-escape"
    escape.symlink_to(forbidden, target_is_directory=True)

    monkeypatch.setenv(
        "RALF_REPAIR_SOURCE_REPO",
        str(canonical),
    )
    monkeypatch.setenv(
        "RALF_REPAIR_ALLOWED_REPOS",
        str(allowed_extra),
    )

    configured_canonical, allowed = _repair_repo_policy()

    assert configured_canonical == canonical.resolve()
    assert _select_repair_repo(
        None,
        canonical=configured_canonical,
        allowed=allowed,
    ) == canonical.resolve()
    assert _select_repair_repo(
        {"repo": str(allowed_extra)},
        canonical=configured_canonical,
        allowed=allowed,
    ) == allowed_extra.resolve()
    assert _select_repair_repo(
        {"repo": str(allowed_alias)},
        canonical=configured_canonical,
        allowed=allowed,
    ) == allowed_extra.resolve()

    for path in (forbidden, prefix_trick, escape):
        with pytest.raises(
            ValueError,
            match="^repair_repo_forbidden$",
        ):
            _select_repair_repo(
                {"repo": str(path)},
                canonical=configured_canonical,
                allowed=allowed,
            )


def test_backend_registers_repair_control_plane_routes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from fastapi import FastAPI

    import ralfloop_agent.repair.approval as approval_module
    import ralfloop_agent.repair.workflow as workflow_module

    canonical_repo = tmp_path / "canonical"
    allowed_repo = tmp_path / "allowed"
    forbidden_repo = tmp_path / "forbidden"
    for path in (
        canonical_repo,
        allowed_repo,
        forbidden_repo,
    ):
        path.mkdir()

    monkeypatch.setenv(
        "RALF_REPAIR_SOURCE_REPO",
        str(canonical_repo),
    )
    monkeypatch.setenv(
        "RALF_REPAIR_ALLOWED_REPOS",
        str(allowed_repo),
    )

    class Record:
        def __init__(self, status):
            self.status = status

        def model_dump(self, mode=None):
            return {
                "run_id": "c" * 32,
                "status": self.status,
            }

    class Store:
        root = tmp_path / "repair" / "records"

        def load(self, run_id):
            assert run_id == "c" * 32
            return Record("planned")

    class Service:
        repair_store = Store()

        def preview(self, run_id):
            return {"status": "preview"}

        def request(self, run_id, requested_by):
            return {
                "status": "approval_requested",
                "request_id": "apr_test",
            }

        def apply(self, request_id):
            return {"status": "executed"}

    service = Service()
    selected_repos: list[Path] = []

    class ApprovalFactory:
        @classmethod
        def from_environment(cls):
            return service

    class Manager:
        def __init__(
            self,
            source_repo,
            *,
            state_root,
        ):
            assert state_root == Store.root.parent
            selected_repos.append(
                Path(source_repo).resolve()
            )

        def plan(self, description):
            assert description == "fix"
            return Record("planned")

        def run(self, description):
            assert description == "fix"
            return Record("approval_pending")

    monkeypatch.setattr(
        approval_module,
        "RepairApprovalService",
        ApprovalFactory,
    )
    monkeypatch.setattr(
        workflow_module,
        "RepairManager",
        Manager,
    )

    app = FastAPI()
    approval_module.register_repair_approval_routes(app)
    endpoints = {
        route.path: route.endpoint
        for route in app.routes
        if hasattr(route, "endpoint")
    }

    response = endpoints["/repairs/plan"](
        {"description": "fix"}
    )
    assert response["status"] == "planned"

    response = endpoints["/repairs/run"](
        {
            "description": "fix",
            "repo": str(allowed_repo),
        }
    )
    assert response["status"] == "approval_pending"

    response = endpoints["/repairs/plan"](
        {
            "description": "fix",
            "repo": str(forbidden_repo),
        }
    )
    assert response == {
        "status": "repair_plan_failed",
        "error": "repair_repo_forbidden",
    }

    response = endpoints["/repairs/{run_id}"]("c" * 32)
    assert response["status"] == "planned"
    assert selected_repos == [
        canonical_repo.resolve(),
        allowed_repo.resolve(),
    ]


def test_repair_snapshot_rechecks_repo_policy_and_clean_source(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from ralfloop_agent.repair.approval import (
        RepairApprovalService,
    )

    repo = _synthetic_repo(tmp_path)
    canonical_repo = tmp_path / "canonical"
    canonical_repo.mkdir()

    manager = RepairManager(
        repo,
        state_root=tmp_path / "state",
        python_executable=sys.executable,
        code_retriever=lambda *args: {
            "ok": True,
            "files": ["router.py"],
        },
        proposer=lambda *args: PATCH,
    )
    record = manager.run("fix matcher")
    assert record.status == "approval_pending"

    monkeypatch.setenv(
        "RALF_REPAIR_SOURCE_REPO",
        str(canonical_repo),
    )
    monkeypatch.setenv(
        "RALF_REPAIR_ALLOWED_REPOS",
        str(repo),
    )
    service = object.__new__(RepairApprovalService)

    scope = service._snapshot(record)
    assert scope["source_repo"] == str(repo.resolve())
    assert len(scope["diff_sha256"]) == 64
    assert len(scope["validation_sha256"]) == 64

    monkeypatch.setenv("RALF_REPAIR_ALLOWED_REPOS", "")
    with pytest.raises(
        ValueError,
        match="^repair_repo_forbidden$",
    ):
        service._snapshot(record)

    monkeypatch.setenv(
        "RALF_REPAIR_ALLOWED_REPOS",
        str(repo),
    )
    (repo / "local-note.txt").write_text(
        "dirty\n",
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="^repair_source_dirty$",
    ):
        service._snapshot(record)



def test_repair_source_blocks_respect_total_character_budget(
    tmp_path,
):
    from ralfloop_agent.repair.workflow import (
        LlamaCppPatchProposer,
    )

    (tmp_path / "a.py").write_text(
        "".join(
            f"value_{i} = {i}\n"
            for i in range(400)
        ),
        encoding="utf-8",
    )

    (tmp_path / "b.py").write_text(
        "".join(
            f"other_{i} = {i}\n"
            for i in range(400)
        ),
        encoding="utf-8",
    )

    proposer = object.__new__(
        LlamaCppPatchProposer
    )
    proposer.max_source_chars = 700

    _originals, sources, _spans = (
        proposer._source_blocks(
            tmp_path,
            ["a.py", "b.py"],
            "Fix a.py value_200",
        )
    )

    assert len(sources) <= 700
    assert 'path="a.py"' in sources


def test_repair_context_preflight_blocks_http_post():
    from types import SimpleNamespace

    import pytest

    from ralfloop_agent.repair.workflow import (
        LlamaCppPatchProposer,
    )

    class Session:
        called = False

        def post(self, *args, **kwargs):
            self.called = True
            raise AssertionError(
                "HTTP POST must not happen"
            )

    proposer = object.__new__(
        LlamaCppPatchProposer
    )
    proposer.config = SimpleNamespace(
        context=100,
        model="fixture",
        base_url="http://127.0.0.1:19091",
    )
    proposer.max_output_tokens = 40
    proposer.context_safety_margin = 20
    proposer.request_timeout_sec = 1.0
    proposer.session = Session()
    proposer._count_input_tokens = (
        lambda _prompt: 50
    )

    with pytest.raises(
        RuntimeError,
        match="repair_context_budget_exceeded",
    ):
        proposer._request_plan(
            "too large",
            ["sample.py"],
        )

    assert proposer.session.called is False


def test_repair_context_preflight_allows_fitting_prompt():
    from types import SimpleNamespace

    from ralfloop_agent.repair.workflow import (
        LlamaCppPatchProposer,
    )

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": (
                                "@@RALF_EDIT "
                                "path=sample.py "
                                "start=1 end=1\n"
                                "value = 2\n"
                                "@@RALF_END\n"
                            )
                        }
                    }
                ]
            }

        def close(self):
            return None

    class Session:
        called = False

        def post(self, *args, **kwargs):
            self.called = True
            return Response()

    proposer = object.__new__(
        LlamaCppPatchProposer
    )
    proposer.config = SimpleNamespace(
        context=200,
        model="fixture",
        base_url="http://127.0.0.1:19091",
    )
    proposer.max_output_tokens = 40
    proposer.context_safety_margin = 20
    proposer.request_timeout_sec = 1.0
    proposer.session = Session()
    proposer._count_input_tokens = (
        lambda _prompt: 50
    )

    result = proposer._request_plan(
        "fits",
        ["sample.py"],
    )

    assert proposer.session.called is True
    assert "@@RALF_EDIT" in result
