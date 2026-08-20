from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping, Sequence

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains.storage import append_jsonl

from .workflow import RepairRecord, RepairStore


CAPABILITY = "bounded_repair_apply"
APPROVAL_ACTION = "repair_apply"
SCHEMA_VERSION = "repair_apply_v1"


def _resolve_repair_repo(value: str | Path) -> Path:
    try:
        return Path(value).expanduser().resolve()
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ValueError("invalid_repair_repo") from exc


def _repair_repo_policy() -> tuple[Path, frozenset[Path]]:
    canonical_raw = os.getenv(
        "RALF_REPAIR_SOURCE_REPO",
        "/home/sibilla-cumana/ralfloop_local_architecture_worktree",
    ).strip()
    canonical = _resolve_repair_repo(
        canonical_raw
        or "/home/sibilla-cumana/ralfloop_local_architecture_worktree"
    )
    allowed = {canonical}

    for raw in os.getenv(
        "RALF_REPAIR_ALLOWED_REPOS",
        "",
    ).split(os.pathsep):
        if raw.strip():
            allowed.add(_resolve_repair_repo(raw.strip()))

    return canonical, frozenset(allowed)


def _select_repair_repo(
    payload: Mapping[str, Any] | None,
    *,
    canonical: Path,
    allowed: frozenset[Path],
) -> Path:
    requested = str((payload or {}).get("repo") or "").strip()
    if not requested:
        return canonical

    resolved = _resolve_repair_repo(requested)
    if resolved not in allowed:
        raise ValueError("repair_repo_forbidden")

    return resolved


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _run(
    argv: Sequence[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    timeout: int = 180,
) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            shell=False,
            check=False,
            timeout=timeout,
        )
        return {
            "command": subprocess.list2cmdline(list(argv)),
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": subprocess.list2cmdline(list(argv)),
            "exit_code": -1,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": f"timeout_after_{timeout}s",
        }


def _git_output(cwd: Path, *args: str) -> str:
    result = _run(("git", *args), cwd, timeout=30)
    if result["exit_code"] != 0:
        raise ValueError(
            "git_probe_failed:"
            + " ".join(args)
            + ":"
            + str(result["stderr"]).strip()[:300]
        )
    return str(result["stdout"]).strip()


def _patch_paths(patch: str) -> list[str]:
    paths: list[str] = []

    for line in patch.splitlines():
        candidate = None

        if line.startswith("+++ b/"):
            candidate = line[6:]
        elif line.startswith("--- a/"):
            candidate = line[6:]

        if not candidate or candidate == "/dev/null":
            continue

        p = Path(candidate)

        if p.is_absolute() or ".." in p.parts:
            raise ValueError("unsafe_patch_path")

        value = p.as_posix()

        if value not in paths:
            paths.append(value)

    if not paths:
        raise ValueError("patch_has_no_paths")

    return paths


class RepairApprovalService:
    """Persistent, hash-bound, one-shot application of an already validated repair."""

    def __init__(
        self,
        store: DomainApprovalStore,
        *,
        policy: DomainApprovalPolicy,
        repair_store: RepairStore | None = None,
        outbox_path: str | Path | None = None,
    ) -> None:
        self.store = store
        self.policy = policy
        self.repair_store = repair_store or RepairStore(
            Path.home() / ".local" / "state" / "ralf" / "repair" / "records"
        )
        configured_outbox = (
            outbox_path
            or os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX")
        )
        self.outbox_path = (
            Path(configured_outbox).expanduser()
            if configured_outbox
            else None
        )

    @classmethod
    def from_environment(cls) -> "RepairApprovalService":
        policy = DomainApprovalPolicy.from_env()
        return cls(
            DomainApprovalStore(policy=policy),
            policy=policy,
        )

    def _load(self, run_id: str) -> RepairRecord:
        record = self.repair_store.load(run_id)

        if record.status not in {
            "approval_pending",
            "approval_requested",
            "applied",
        }:
            raise ValueError("repair_not_ready_for_approval")

        if not record.worktree:
            raise ValueError("repair_worktree_missing")

        if not record.diff.strip():
            raise ValueError("repair_patch_empty")

        if not record.validation.get("ok"):
            raise ValueError("repair_validation_not_ok")

        if not record.post_tests or any(
            int(item.get("exit_code", 1)) != 0
            for item in record.post_tests
        ):
            raise ValueError("repair_post_tests_not_ok")

        return record

    def _snapshot(self, record: RepairRecord) -> dict[str, Any]:
        source = Path(record.source_repo).expanduser().resolve()
        worktree = Path(str(record.worktree)).expanduser().resolve()

        if source == worktree:
            raise ValueError("repair_source_equals_worktree")

        if not source.is_dir() or not worktree.is_dir():
            raise ValueError("repair_paths_missing")

        _, allowed_repos = _repair_repo_policy()
        if source not in allowed_repos:
            raise ValueError("repair_repo_forbidden")

        source_status = _run(
            (
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ),
            source,
            timeout=30,
        )
        if source_status["exit_code"] != 0:
            raise ValueError("repair_source_status_failed")
        if str(source_status["stdout"]).strip():
            raise ValueError("repair_source_dirty")

        source_head = _git_output(source, "rev-parse", "HEAD")
        worktree_head = _git_output(worktree, "rev-parse", "HEAD")

        if source_head != worktree_head:
            raise ValueError("source_head_changed_since_repair")

        patch = record.diff
        changed_files = _patch_paths(patch)

        # Il patch deve essere realmente presente nel worktree isolato.
        reverse_check = _run(
            ("git", "apply", "--reverse", "--check", "-"),
            worktree,
            input_text=patch,
            timeout=60,
        )
        if reverse_check["exit_code"] != 0:
            raise ValueError("isolated_worktree_patch_mismatch")

        validation_binding = {
            "validation": record.validation,
            "post_tests": record.post_tests,
            "selected_files": record.selected_files,
        }

        return {
            "action": APPROVAL_ACTION,
            "schema_version": SCHEMA_VERSION,
            "capability": CAPABILITY,
            "run_id": record.run_id,
            "source_repo": str(source),
            "worktree": str(worktree),
            "base_commit": worktree_head,
            "diff_sha256": _sha256_text(patch),
            "changed_files": changed_files,
            "validation_sha256": _json_hash(validation_binding),
        }

    def preview(self, run_id: str) -> dict[str, Any]:
        record = self._load(run_id)
        scope = self._snapshot(record)

        source = Path(scope["source_repo"])
        apply_check = _run(
            ("git", "apply", "--check", "-"),
            source,
            input_text=record.diff,
            timeout=60,
        )

        return {
            "status": "preview",
            "capability": CAPABILITY,
            "approval_required": True,
            "run_id": run_id,
            "source_repo": scope["source_repo"],
            "base_commit": scope["base_commit"],
            "diff_sha256": scope["diff_sha256"],
            "changed_files": scope["changed_files"],
            "validation_sha256": scope["validation_sha256"],
            "scope_digest": scope_digest(scope),
            "apply_check_ok": apply_check["exit_code"] == 0,
            "apply_check_stderr": str(apply_check["stderr"]).strip()[:500],
            "arbitrary_shell": False,
        }

    def request(self, run_id: str, *, requested_by: str) -> dict[str, Any]:
        if not self.policy.enabled:
            return {
                "status": "approval_gate_disabled",
                "approval_required": True,
            }

        if not self.policy.allowed_user_ids or not self.policy.allowed_chat_ids:
            return {
                "status": "approval_allowlist_unconfigured",
                "approval_required": True,
            }

        record = self._load(run_id)
        scope = self._snapshot(record)

        apply_check = _run(
            ("git", "apply", "--check", "-"),
            Path(scope["source_repo"]),
            input_text=record.diff,
            timeout=60,
        )

        if apply_check["exit_code"] != 0:
            return {
                "status": "repair_apply_check_failed",
                "approval_required": True,
                "stderr": str(apply_check["stderr"]).strip()[:500],
            }

        out = self.store.create_request(
            action=APPROVAL_ACTION,
            bando_id=f"repair:{run_id}",
            version=SCHEMA_VERSION,
            scope=scope,
            requested_by=requested_by,
        )

        request = (
            out.get("request")
            if isinstance(out, Mapping)
            else None
        )

        request_id = str(
            out.get("request_id")
            or (
                request.get("request_id")
                if isinstance(request, Mapping)
                else ""
            )
            or ""
        )

        notification_queued = False

        if request_id and self.outbox_path is not None:
            try:
                append_jsonl(
                    self.outbox_path,
                    {
                        "status": "queued",
                        "request_id": request_id,
                        "api_url": self.policy.api_url,
                        "message": str(
                            request.get("telegram_message") or ""
                        )
                        if isinstance(request, Mapping)
                        else "",
                    },
                )
            except OSError:
                self.store.cancel(request_id)
                return {
                    "status": "approval_notification_failed",
                    "approval_required": True,
                    "request_id": request_id,
                    "notification_queued": False,
                }

            notification_queued = True

        if request_id:
            record.approval_request_id = request_id
            record.approval_status = "pending_user"
            record.status = "approval_requested"
            self.repair_store.save(record)

        if isinstance(out, Mapping):
            return {
                **out,
                "notification_queued": notification_queued,
            }

        return out

    def apply(self, request_id: str) -> dict[str, Any]:
        if not self.policy.enabled:
            return {
                "status": "approval_gate_disabled",
                "request_id": request_id,
                "applied": False,
            }

        row = self.store.get_request(request_id)

        if not row:
            return {
                "status": "not_found",
                "request_id": request_id,
                "applied": False,
            }

        effective = effective_approval_status(row)

        if effective == "consumed":
            return {
                "status": "already_executed",
                "request_id": request_id,
                "applied": False,
            }

        if effective == "executing":
            return {
                "status": "execution_outcome_pending",
                "request_id": request_id,
                "applied": False,
            }

        if effective == "execution_failed":
            return {
                "status": "execution_failed",
                "request_id": request_id,
                "applied": False,
                "retry_allowed": False,
            }

        if effective != "approved":
            return {
                "status": "approval_required",
                "request_id": request_id,
                "applied": False,
                "effective_status": effective,
            }

        if row.get("action") != APPROVAL_ACTION:
            return {
                "status": "action_mismatch",
                "request_id": request_id,
                "applied": False,
            }

        stored_scope = (
            row.get("scope")
            if isinstance(row.get("scope"), Mapping)
            else {}
        )

        run_id = str(stored_scope.get("run_id") or "")

        try:
            record = self._load(run_id)
            current_scope = self._snapshot(record)
        except (KeyError, OSError, ValueError) as exc:
            self.store.mark_stale(
                request_id,
                [f"repair_snapshot_invalid:{type(exc).__name__}"],
            )
            return {
                "status": "stale",
                "request_id": request_id,
                "applied": False,
                "reason": str(exc),
            }

        bound_keys = (
            "schema_version",
            "capability",
            "run_id",
            "source_repo",
            "worktree",
            "base_commit",
            "diff_sha256",
            "changed_files",
            "validation_sha256",
        )

        stale = [
            key
            for key in bound_keys
            if stored_scope.get(key) != current_scope.get(key)
        ]

        if stale:
            self.store.mark_stale(
                request_id,
                ["repair_binding_changed:" + key for key in stale],
            )
            return {
                "status": "stale",
                "request_id": request_id,
                "applied": False,
                "changed_bindings": stale,
            }

        source = Path(current_scope["source_repo"])
        patch = record.diff

        # Ultimo check read-only PRIMA del claim.
        precheck = _run(
            ("git", "apply", "--check", "-"),
            source,
            input_text=patch,
            timeout=60,
        )

        if precheck["exit_code"] != 0:
            self.store.mark_stale(
                request_id,
                ["git_apply_check_failed"],
            )
            return {
                "status": "stale",
                "request_id": request_id,
                "applied": False,
                "stderr": str(precheck["stderr"]).strip()[:500],
            }

        claim = self.store.claim_execution(
            request_id,
            action=APPROVAL_ACTION,
        )

        if not claim.get("claimed"):
            return {
                "status": str(
                    claim.get("status") or "execution_not_claimed"
                ),
                "request_id": request_id,
                "applied": False,
            }

        applied = _run(
            ("git", "apply", "-"),
            source,
            input_text=patch,
            timeout=60,
        )

        if applied["exit_code"] != 0:
            return self._finish_failure(
                request_id,
                record,
                "git_apply_failed",
                str(applied["stderr"]).strip()[:500],
            )

        verification: list[dict[str, Any]] = []

        verification.append(
            _run(
                ("git", "apply", "--reverse", "--check", "-"),
                source,
                input_text=patch,
                timeout=60,
            )
        )

        verification.append(
            _run(
                ("git", "diff", "--check"),
                source,
                timeout=60,
            )
        )

        py_files = [
            str(source / path)
            for path in current_scope["changed_files"]
            if str(path).endswith(".py")
            and (source / str(path)).exists()
        ]

        if py_files:
            verification.append(
                _run(
                    (sys.executable, "-m", "py_compile", *py_files),
                    source,
                    timeout=120,
                )
            )

        if any(item["exit_code"] != 0 for item in verification):
            rollback_check = _run(
                ("git", "apply", "--reverse", "--check", "-"),
                source,
                input_text=patch,
                timeout=60,
            )

            rollback = {
                "status": "not_attempted",
                "exit_code": rollback_check["exit_code"],
                "stderr": rollback_check["stderr"],
            }

            if rollback_check["exit_code"] == 0:
                rollback = _run(
                    ("git", "apply", "--reverse", "-"),
                    source,
                    input_text=patch,
                    timeout=60,
                )

            return self._finish_failure(
                request_id,
                record,
                "post_apply_verification_failed",
                json.dumps(
                    {
                        "verification": verification,
                        "rollback": rollback,
                    },
                    ensure_ascii=False,
                    default=str,
                )[:2000],
            )

        result = {
            "status": "executed",
            "request_id": request_id,
            "capability": CAPABILITY,
            "run_id": run_id,
            "source_repo": str(source),
            "base_commit": current_scope["base_commit"],
            "diff_sha256": current_scope["diff_sha256"],
            "changed_files": current_scope["changed_files"],
            "applied": True,
            "verified": True,
            "retry_allowed": False,
        }

        finalized = self.store.finish_claimed_execution(
            request_id,
            action=APPROVAL_ACTION,
            success=True,
            result=result,
        )

        record.approval_request_id = request_id
        record.approval_status = str(
            finalized.get("status") or "execution_finalization_unknown"
        )
        record.status = "applied"
        record.apply_result = result
        self.repair_store.save(record)

        if finalized.get("status") != "consumed":
            return {
                **result,
                "status": "execution_finalization_failed",
                "applied": True,
                "verified": True,
                "retry_allowed": False,
                "finalization": finalized,
            }

        return result

    def _finish_failure(
        self,
        request_id: str,
        record: RepairRecord,
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        result = {
            "status": "execution_failed",
            "request_id": request_id,
            "capability": CAPABILITY,
            "run_id": record.run_id,
            "applied": False,
            "retry_allowed": False,
            "reason": reason,
            "detail": detail,
        }

        self.store.finish_claimed_execution(
            request_id,
            action=APPROVAL_ACTION,
            success=False,
            result=result,
        )

        record.approval_request_id = request_id
        record.approval_status = "execution_failed"
        record.apply_result = result
        self.repair_store.save(record)

        return result


def register_repair_approval_routes(app: Any) -> None:
    service = RepairApprovalService.from_environment()
    canonical_repo, allowed_repos = _repair_repo_policy()

    def _manager(source_repo: Path):
        from ralfloop_agent.repair.workflow import RepairManager

        return RepairManager(
            source_repo,
            state_root=service.repair_store.root.parent,
        )

    @app.post("/repairs/plan")
    def repair_plan(
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            source_repo = _select_repair_repo(
                payload,
                canonical=canonical_repo,
                allowed=allowed_repos,
            )
        except ValueError as exc:
            return {
                "status": "repair_plan_failed",
                "error": str(exc),
            }

        description = str(
            (payload or {}).get("description") or ""
        ).strip()

        if not description:
            return {
                "status": "repair_plan_failed",
                "error": "missing_repair_description",
            }

        try:
            return _manager(source_repo).plan(description).model_dump(
                mode="json"
            )
        except (KeyError, OSError, ValueError, RuntimeError) as exc:
            return {
                "status": "repair_plan_failed",
                "error": str(exc),
            }

    @app.post("/repairs/run")
    def repair_run(
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            source_repo = _select_repair_repo(
                payload,
                canonical=canonical_repo,
                allowed=allowed_repos,
            )
        except ValueError as exc:
            return {
                "status": "repair_run_failed",
                "error": str(exc),
            }

        description = str(
            (payload or {}).get("description") or ""
        ).strip()

        if not description:
            return {
                "status": "repair_run_failed",
                "error": "missing_repair_description",
            }

        try:
            return _manager(source_repo).run(description).model_dump(
                mode="json"
            )
        except (KeyError, OSError, ValueError, RuntimeError) as exc:
            return {
                "status": "repair_run_failed",
                "error": str(exc),
            }

    @app.get("/repairs/{run_id}")
    def repair_status(run_id: str) -> dict[str, Any]:
        try:
            return service.repair_store.load(run_id).model_dump(
                mode="json"
            )
        except (KeyError, OSError, ValueError) as exc:
            return {
                "status": "repair_status_failed",
                "run_id": run_id,
                "error": str(exc),
            }

    @app.get("/repairs/{run_id}/approval-preview")
    async def repair_approval_preview(
        run_id: str,
    ) -> dict[str, Any]:
        try:
            return service.preview(run_id)
        except (KeyError, OSError, ValueError) as exc:
            return {
                "status": "repair_preview_failed",
                "run_id": run_id,
                "error": str(exc),
                "approval_required": True,
            }

    @app.post("/repairs/{run_id}/approval-request")
    async def repair_approval_request(
        run_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return service.request(
                run_id,
                requested_by=str(
                    (payload or {}).get("requested_by")
                    or "ralf_repair_api"
                ),
            )
        except (KeyError, OSError, ValueError) as exc:
            return {
                "status": "repair_approval_request_failed",
                "run_id": run_id,
                "error": str(exc),
                "approval_required": True,
            }

    @app.post("/repair-approvals/{request_id}/apply")
    async def repair_approval_apply(
        request_id: str,
    ) -> dict[str, Any]:
        return service.apply(request_id)


__all__ = [
    "APPROVAL_ACTION",
    "CAPABILITY",
    "RepairApprovalService",
    "SCHEMA_VERSION",
    "register_repair_approval_routes",
]
