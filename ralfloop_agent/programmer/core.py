from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
import json
import os
from pathlib import Path
import pwd
import subprocess
from typing import Any, Callable, Mapping
from uuid import uuid4

from ralfloop_agent.coding_harness.harness import HarnessConfig


HarnessRunner = Callable[[HarnessConfig], Mapping[str, Any]]


class ProgrammerState(StrEnum):
    BLOCKED = "blocked"
    CANDIDATE_READY = "candidate_ready"
    FAILED = "failed"
    FAIL_CLOSED = "fail_closed"


@dataclass(frozen=True)
class ProgrammerConfig:
    workdir: Path
    task: str
    validator_command: str = "git diff --check"
    allowed_roots: tuple[Path, ...] = ()
    state_root: Path | None = None
    require_clean_worktree: bool = True
    require_linked_worktree: bool = True
    allow_test_changes: bool = False
    protected_globs: tuple[str, ...] = ()
    allow_worker_shell: bool = False
    worker_user: str | None = None
    provider: str | None = None
    model: str | None = None
    fallback_provider: str | None = None
    fallback_model: str | None = None

    @classmethod
    def from_environment(
        cls,
        *,
        workdir: str | Path,
        task: str,
        validator_command: str = "git diff --check",
        allow_test_changes: bool = False,
        protected_globs: tuple[str, ...] = (),
        allow_worker_shell: bool = False,
    ) -> "ProgrammerConfig":
        raw_roots = os.environ.get("RALF_CODE_WORKTREE_ROOTS", "")
        roots = tuple(
            Path(item).expanduser()
            for item in raw_roots.split(":")
            if item.strip()
        )
        raw_state = os.environ.get("RALF_PROGRAMMER_STATE_ROOT", "").strip()
        state_root = Path(raw_state).expanduser() if raw_state else None
        worker_user = os.environ.get("RALF_CODE_WORKER_USER", "").strip() or None
        provider = os.environ.get("RALF_CODE_PROVIDER", "").strip() or None
        model = os.environ.get("RALF_CODE_MODEL", "").strip() or None
        fallback_provider = os.environ.get("RALF_CODE_FALLBACK_PROVIDER", "").strip() or None
        fallback_model = os.environ.get("RALF_CODE_FALLBACK_MODEL", "").strip() or None
        return cls(
            workdir=Path(workdir),
            task=task,
            validator_command=validator_command,
            allowed_roots=roots,
            state_root=state_root,
            allow_test_changes=allow_test_changes,
            protected_globs=protected_globs,
            allow_worker_shell=allow_worker_shell,
            worker_user=worker_user,
            provider=provider,
            model=model,
            fallback_provider=fallback_provider,
            fallback_model=fallback_model,
        )


@dataclass(frozen=True)
class ProgrammerResult:
    run_id: str
    state: ProgrammerState
    reason: str
    workdir: str
    branch: str | None
    base_head: str | None
    final_head: str | None
    report: Mapping[str, Any]
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["state"] = self.state.value
        return data


@dataclass(frozen=True)
class _Preflight:
    ok: bool
    reason: str
    workdir: Path
    branch: str | None = None
    head: str | None = None


def _inside(root: Path, candidate: Path) -> bool:
    return candidate == root or candidate.is_relative_to(root)


def _git(workdir: Path, *args: str, timeout: int = 8) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        check=False,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


def _git_path(workdir: Path, name: str) -> Path | None:
    result = _git(workdir, "rev-parse", "--git-path", name)
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    return path if path.is_absolute() else (workdir / path).resolve()


class ProgrammerAgent:
    """Bounded coding orchestrator. It never commits, pushes, or deploys."""

    def __init__(self, config: ProgrammerConfig, *, runner: HarnessRunner | None = None) -> None:
        self.config = config
        self.runner = runner

    def preflight(self) -> _Preflight:
        task = self.config.task.strip()
        validator = self.config.validator_command.strip()
        requested = self.config.workdir.expanduser()
        try:
            workdir = requested.resolve()
        except OSError:
            return _Preflight(False, "workdir_unresolvable", requested)
        if not task:
            return _Preflight(False, "task_empty", workdir)
        if not validator:
            return _Preflight(False, "validator_empty", workdir)
        if not workdir.is_dir():
            return _Preflight(False, "workdir_missing", workdir)

        roots: list[Path] = []
        for item in self.config.allowed_roots:
            try:
                roots.append(item.expanduser().resolve())
            except OSError:
                continue
        if not roots:
            return _Preflight(False, "allowed_roots_missing", workdir)
        if not any(_inside(root, workdir) for root in roots):
            return _Preflight(False, "workdir_outside_allowlist", workdir)

        git_marker = workdir / ".git"
        if self.config.require_linked_worktree and not git_marker.is_file():
            return _Preflight(False, "linked_git_worktree_required", workdir)

        top = _git(workdir, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return _Preflight(False, "git_worktree_required", workdir)
        try:
            top_path = Path(top.stdout.strip()).resolve()
        except OSError:
            return _Preflight(False, "git_root_unresolvable", workdir)
        if top_path != workdir:
            return _Preflight(False, "workdir_must_be_git_root", workdir)

        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG"):
            path = _git_path(workdir, marker)
            if path is not None and path.exists():
                return _Preflight(False, f"git_operation_in_progress:{marker.lower()}", workdir)
        for marker in ("rebase-merge", "rebase-apply"):
            path = _git_path(workdir, marker)
            if path is not None and path.exists():
                return _Preflight(False, f"git_operation_in_progress:{marker}", workdir)

        status = _git(workdir, "status", "--porcelain=v1", "--untracked-files=all")
        if status.returncode != 0:
            return _Preflight(False, "git_status_failed", workdir)
        if self.config.require_clean_worktree and status.stdout.strip():
            return _Preflight(False, "worktree_dirty", workdir)

        head = _git(workdir, "rev-parse", "HEAD")
        if head.returncode != 0:
            return _Preflight(False, "git_head_unavailable", workdir)
        branch_result = _git(workdir, "symbolic-ref", "--short", "-q", "HEAD")
        branch = branch_result.stdout.strip() or None
        return _Preflight(True, "ready", workdir, branch=branch, head=head.stdout.strip())

    def _harness_config(self, workdir: Path) -> HarnessConfig:
        config = HarnessConfig(
            workdir=workdir,
            task=self.config.task.strip(),
            validator_command=self.config.validator_command.strip(),
            allow_test_changes=self.config.allow_test_changes,
            protected_globs=self.config.protected_globs,
        )
        updates: dict[str, Any] = {
            "worker_tools": (
                ("read", "edit", "write", "bash")
                if self.config.allow_worker_shell
                else ("read", "edit", "write")
            )
        }
        if self.config.worker_user:
            updates["worker_user"] = self.config.worker_user
        if self.config.provider:
            updates["provider"] = self.config.provider
        if self.config.model:
            updates["model"] = self.config.model
        if self.config.fallback_provider:
            updates["fallback_provider"] = self.config.fallback_provider
        if self.config.fallback_model:
            updates["fallback_model"] = self.config.fallback_model
        return replace(config, **updates)

    def _state_root(self) -> Path:
        if self.config.state_root is not None:
            return self.config.state_root.expanduser().resolve()
        raw = os.environ.get("RALF_PROGRAMMER_STATE_ROOT", "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        user_home = Path(pwd.getpwuid(os.geteuid()).pw_dir)
        return user_home / ".local" / "state" / "ralf" / "programmer"

    def _persist(self, result: ProgrammerResult) -> None:
        root = self._state_root() / "runs"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = root / f"{result.run_id}.json"
        temporary = root / f".{result.run_id}.{uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(target)

    def run(self) -> ProgrammerResult:
        run_id = "programmer." + uuid4().hex
        created_at = datetime.now(UTC).isoformat()
        preflight = self.preflight()
        if not preflight.ok:
            result = ProgrammerResult(
                run_id=run_id,
                state=ProgrammerState.BLOCKED,
                reason=preflight.reason,
                workdir=str(preflight.workdir),
                branch=preflight.branch,
                base_head=preflight.head,
                final_head=preflight.head,
                report={},
                created_at=created_at,
            )
            self._persist(result)
            return result

        try:
            runner = self.runner
            if runner is None:
                from ralfloop_agent.coding_harness import harness as coding_harness

                runner = coding_harness.run_harness
            report = dict(runner(self._harness_config(preflight.workdir)))
        except Exception as exc:
            result = ProgrammerResult(
                run_id=run_id,
                state=ProgrammerState.FAIL_CLOSED,
                reason=f"harness_error:{type(exc).__name__}",
                workdir=str(preflight.workdir),
                branch=preflight.branch,
                base_head=preflight.head,
                final_head=preflight.head,
                report={"error": str(exc)[:500]},
                created_at=created_at,
            )
            self._persist(result)
            return result

        final = _git(preflight.workdir, "rev-parse", "HEAD")
        final_head = final.stdout.strip() if final.returncode == 0 else None
        if final_head != preflight.head:
            state = ProgrammerState.FAIL_CLOSED
            reason = "worker_changed_head"
        else:
            final_status = str(report.get("final_status") or "").strip()
            if final_status == "pass":
                state = ProgrammerState.CANDIDATE_READY
                reason = str(report.get("decision") or "harness_pass")
            elif final_status == "fail_closed":
                state = ProgrammerState.FAIL_CLOSED
                reason = str(report.get("decision") or "harness_fail_closed")
            else:
                state = ProgrammerState.FAILED
                reason = str(report.get("decision") or final_status or "harness_failed")

        result = ProgrammerResult(
            run_id=run_id,
            state=state,
            reason=reason,
            workdir=str(preflight.workdir),
            branch=preflight.branch,
            base_head=preflight.head,
            final_head=final_head,
            report=report,
            created_at=created_at,
        )
        self._persist(result)
        return result


__all__ = [
    "ProgrammerAgent",
    "ProgrammerConfig",
    "ProgrammerResult",
    "ProgrammerState",
]
