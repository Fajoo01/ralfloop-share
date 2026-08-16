from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field
import requests

from ralfloop_agent.model_tools import ModelToolManager, ModelToolRegistry
from ralfloop_agent.model_tools.registry import DEFAULT_HF_CACHE
from ralfloop_agent.providers.llama_cpp_server import LlamaCppServerConfig

from .validator import PatchValidator


class RepairRecord(BaseModel):
    run_id: str
    action: str
    description: str
    status: str
    source_repo: str
    worktree: str | None = None
    created_at: str
    updated_at: str
    selected_files: list[str] = Field(default_factory=list)
    code_retriever: dict[str, Any] = Field(default_factory=dict)
    pre_tests: list[dict[str, Any]] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    post_tests: list[dict[str, Any]] = Field(default_factory=list)
    diff: str = ""
    approval_required: bool = False
    approval_status: str | None = None
    approval_request_id: str | None = None
    apply_result: dict[str, Any] = Field(default_factory=dict)
    rollback: list[str] = Field(default_factory=list)
    error: str | None = None


CodeRetriever = Callable[[Path, str], dict[str, Any]]
PatchProposer = Callable[[Path, str, list[str]], str]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _command(
    argv: list[str],
    cwd: Path,
    *,
    timeout: int = 120,
    input_text: str | None = None,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            input=input_text,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env_overrides or {})},
        )
        return {
            "command": subprocess.list2cmdline(argv),
            "exit_code": result.returncode,
            "stdout": result.stdout[-20000:],
            "stderr": result.stderr[-20000:],
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": subprocess.list2cmdline(argv),
            "exit_code": -1,
            "stdout": exc.stdout if isinstance(exc.stdout, str) else "",
            "stderr": f"timeout_after_{timeout}s",
        }


class RepairStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def save(self, record: RepairRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.root / f"{record.run_id}.json"
        temporary = self.root / f".{record.run_id}.{uuid4().hex}.tmp"
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(target)

    def load(self, run_id: str) -> RepairRecord:
        if not re.fullmatch(r"[a-f0-9]{32}", run_id):
            raise ValueError("invalid_repair_run_id")
        try:
            return RepairRecord.model_validate_json((self.root / f"{run_id}.json").read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError("repair_run_not_found") from exc


class LocalModelToolCodeRetriever:
    def __init__(self, manager: ModelToolManager | None = None) -> None:
        shared_cache = Path("/home/sibilla-cumana/.cache/huggingface/hub")
        registry = ModelToolRegistry.load(
            cache_root=shared_cache if shared_cache.is_dir() else DEFAULT_HF_CACHE
        )
        self.manager = manager or ModelToolManager(registry)
        self.registry = registry

    def __call__(self, worktree: Path, description: str) -> dict[str, Any]:
        listed = _command(["git", "ls-files", "*.py"], worktree, timeout=20)
        if listed["exit_code"] != 0:
            return {
                "ok": False,
                "error_type": "repository_scan_failed",
                "files": [],
                "evidence": listed,
            }

        documents = []
        search_terms = {
            token
            for token in re.findall(r"[a-zA-Z0-9_]{3,}", description.casefold())
            if len(token) >= 3
        }

        tracked_files = set(listed["stdout"].splitlines())
        explicit_paths = []
        for candidate in re.findall(
            r"(?:ralfloop_agent|tests|src|tools)/[A-Za-z0-9_./-]+\.py",
            description,
        ):
            candidate = candidate.strip("/")
            if (
                candidate in tracked_files
                and candidate not in explicit_paths
            ):
                explicit_paths.append(candidate)

        for raw in listed["stdout"].splitlines():
            path = (worktree / raw).resolve()
            try:
                path.relative_to(worktree.resolve())
            except ValueError:
                continue
            if path.is_symlink() or not path.is_file():
                continue
            try:
                source = path.read_text(encoding="utf-8")[:6000]
            except (OSError, UnicodeError):
                continue

            haystack = (raw + "\n" + source).casefold()
            lexical_score = sum(
                min(haystack.count(term), 8)
                for term in search_terms
            )

            documents.append(
                {
                    "document_id": raw,
                    "text": source,
                    "_lexical_score": lexical_score,
                }
            )

        # Semantic retrieval remains the final selector, but only over a
        # deterministic bounded candidate set.
        documents.sort(
            key=lambda item: (
                -int(item["_lexical_score"]),
                str(item["document_id"]),
            )
        )
        documents_by_id = {
            str(item["document_id"]): item
            for item in documents
        }
        pinned_documents = [
            documents_by_id[path]
            for path in explicit_paths
            if path in documents_by_id
        ]
        remaining_documents = [
            item
            for item in documents
            if str(item["document_id"]) not in explicit_paths
        ]
        documents = (pinned_documents + remaining_documents)[:16]

        for item in documents:
            item.pop("_lexical_score", None)

        if not documents:
            return {
                "ok": False,
                "error_type": "no_code_documents",
                "files": [],
            }

        candidates = []

        primary = self.registry.for_capability("retrieve_code_context")
        if primary is not None:
            candidates.append(primary)

        try:
            fallback = self.registry.get("semantic_retriever_bge_m3_v1")
        except KeyError:
            fallback = None

        if fallback is not None and all(
            item.tool_id != fallback.tool_id for item in candidates
        ):
            candidates.append(fallback)

        attempts = []

        for spec in candidates:
            availability = self.registry.availability(spec)
            attempts.append(
                {
                    "tool_id": spec.tool_id,
                    "availability": availability.status,
                }
            )

            if not availability.ok:
                continue

            tool_payload = {
                "query": description,
                "documents": documents,
            }
            properties = dict(spec.input_schema.get("properties") or {})
            if "top_k" in properties:
                tool_payload["top_k"] = 8

            envelope = self.manager.invoke(
                spec.tool_id,
                tool_payload,
            )

            attempts[-1]["invoke_ok"] = envelope.ok
            attempts[-1]["error_type"] = envelope.error_type

            if not envelope.ok:
                continue

            ranked_files = [
                str(row["document_id"])
                for row in envelope.output.get("ranked", [])
            ]
            files = []
            for candidate in [*explicit_paths, *ranked_files]:
                if candidate not in files:
                    files.append(candidate)
            files = files[:5]

            if files:
                return {
                    "ok": True,
                    "error_type": None,
                    "files": files,
                    "selected_tool": spec.tool_id,
                    "attempts": attempts,
                    "result_envelope": envelope.model_dump(mode="json"),
                }

        error_type = (
            attempts[-1].get("error_type")
            or attempts[-1].get("availability")
            if attempts
            else "tool_unavailable"
        )

        return {
            "ok": False,
            "error_type": error_type or "code_retriever_unavailable",
            "files": [],
            "attempts": attempts,
        }

class LlamaCppPatchProposer:
    def __init__(self, *, session: requests.Session | None = None) -> None:
        self.config = LlamaCppServerConfig.from_env()
        self.session = session or requests.Session()

    def __call__(self, worktree: Path, description: str, selected_files: list[str]) -> str:
        if self.config.fallback != "none":
            raise RuntimeError("repair_requires_llama_cpp_without_fallback")
        sources = []
        for raw in selected_files[:5]:
            path = (worktree / raw).resolve()
            path.relative_to(worktree.resolve())
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"invalid_selected_file:{raw}")
            sources.append(f"<file path={json.dumps(raw)}>\n{path.read_text(encoding='utf-8')[:20000]}\n</file>")
        prompt = (
            "Produce only one unified git diff. Do not add prose or markdown fences. "
            "Modify at most five supplied text files. Never bypass approval, touch credentials, services, .git, "
            "Telegram, RecursiveMAS, checkpoints, models, or main_plugin.py.\n\n"
            f"Problem:\n{description[:8000]}\n\n" + "\n".join(sources)
        )
        response = self.session.post(
            f"{self.config.base_url}/v1/chat/completions",
            json={
                "model": self.config.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": "You propose bounded code patches; validators decide whether they are safe."},
                    {"role": "user", "content": prompt},
                ],
            },
            timeout=(2.0, min(self.config.request_timeout_sec, 180.0)),
        )
        try:
            response.raise_for_status()
            payload = response.json()
        finally:
            response.close()
        try:
            patch = str(payload["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("llama_cpp_patch_response_invalid") from exc
        if patch.startswith("```"):
            lines = patch.splitlines()
            if lines and lines[-1].strip() == "```":
                patch = "\n".join(lines[1:-1]).removeprefix("diff\n")
        return patch


class RepairManager:
    def __init__(
        self,
        source_repo: str | Path,
        *,
        state_root: str | Path = Path.home() / ".local" / "state" / "ralf" / "repair",
        python_executable: str = sys.executable,
        code_retriever: CodeRetriever | None = None,
        proposer: PatchProposer | None = None,
        validator: PatchValidator | None = None,
    ) -> None:
        self.source_repo = Path(source_repo).resolve()
        self.state_root = Path(state_root).expanduser().resolve()
        self.store = RepairStore(self.state_root / "records")
        self.python_executable = python_executable
        self.code_retriever = code_retriever or LocalModelToolCodeRetriever()
        self.proposer = proposer or LlamaCppPatchProposer()
        self.validator = validator or PatchValidator()
        probe = _command(["git", "rev-parse", "--show-toplevel"], self.source_repo, timeout=10)
        if probe["exit_code"] != 0 or Path(probe["stdout"].strip()).resolve() != self.source_repo:
            raise ValueError("repair_source_must_be_git_root")

    def _new_record(self, action: str, description: str) -> RepairRecord:
        now = _now()
        return RepairRecord(
            run_id=uuid4().hex,
            action=action,
            description=description,
            status="planned" if action == "plan" else "initializing",
            source_repo=str(self.source_repo),
            created_at=now,
            updated_at=now,
        )

    def plan(self, description: str) -> RepairRecord:
        record = self._new_record("plan", description)
        record.validation = {
            "max_files": self.validator.max_files,
            "max_changed_lines": self.validator.max_changed_lines,
            "allowed_suffixes": sorted(self.validator.allowed_suffixes),
            "production_write": False,
            "commit": False,
            "deploy": False,
        }
        record.rollback = ["No worktree created; no rollback required."]
        self.store.save(record)
        return record

    def _tests(self, worktree: Path) -> list[list[str]]:
        tests = list((worktree / "tests").glob("test_*.py")) if (worktree / "tests").is_dir() else []
        if not tests or len(tests) > 50:
            return []
        return [[self.python_executable, "-m", "pytest", "-q"]]

    def run(self, description: str) -> RepairRecord:
        record = self._new_record("run", description)
        run_root = self.state_root / "runs" / record.run_id
        worktree = run_root / "worktree"
        run_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        record.worktree = str(worktree)
        record.rollback = [
            f"git -C {self.source_repo} worktree remove {worktree}",
            f"Review and then remove runtime record {self.store.root / (record.run_id + '.json')}",
        ]
        self.store.save(record)
        added = _command(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            self.source_repo,
            timeout=60,
        )
        if added["exit_code"] != 0:
            record.status = "failed"
            record.error = "isolated_worktree_creation_failed"
            record.validation = {"worktree_add": added}
            record.updated_at = _now()
            self.store.save(record)
            return record
        record.status = "diagnosing"
        test_commands = self._tests(worktree)
        record.pre_tests = [_command(command, worktree) for command in test_commands]
        retrieval = self.code_retriever(worktree, description)
        record.code_retriever = retrieval
        record.selected_files = [str(path) for path in retrieval.get("files", [])[: self.validator.max_files]]
        if not retrieval.get("ok") or not record.selected_files:
            record.status = "blocked"
            record.error = str(retrieval.get("error_type") or "code_retriever_unavailable")
            record.updated_at = _now()
            self.store.save(record)
            return record
        try:
            patch = self.proposer(worktree, description, record.selected_files)
        except Exception as exc:
            record.status = "failed"
            record.error = f"patch_proposal_failed:{type(exc).__name__}:{exc}"
            record.updated_at = _now()
            self.store.save(record)
            return record
        bounded_validator = PatchValidator(
            max_files=self.validator.max_files,
            max_changed_lines=self.validator.max_changed_lines,
            allowed_paths=tuple(record.selected_files),
            allowed_suffixes=self.validator.allowed_suffixes,
        )
        validation = bounded_validator.validate(patch, worktree)
        record.validation = {
            "ok": validation.ok,
            "files": list(validation.files),
            "changed_lines": validation.changed_lines,
            "errors": list(validation.errors),
        }
        if not validation.ok:
            record.status = "failed"
            record.error = "patch_validation_failed"
            record.updated_at = _now()
            self.store.save(record)
            return record
        applied = _command(["git", "apply", "-"], worktree, timeout=20, input_text=patch)
        if applied["exit_code"] != 0:
            record.status = "failed"
            record.error = "patch_apply_failed"
            record.validation["apply"] = applied
            record.updated_at = _now()
            self.store.save(record)
            return record
        changed = [path for path in validation.files if path.endswith(".py")]
        checks: list[dict[str, Any]] = []
        if changed:
            checks.append(
                _command(
                    [self.python_executable, "-m", "py_compile", *changed],
                    worktree,
                    timeout=60,
                    env_overrides={"PYTHONPYCACHEPREFIX": str(run_root / "pycache")},
                )
            )
        checks.extend(_command(command, worktree) for command in test_commands)
        checks.append(_command(["git", "diff", "--check"], worktree, timeout=20))
        record.post_tests = checks
        # Conserva il patch validato originale: include anche file nuovi/untracked.
        record.diff = patch
        if not checks or any(check["exit_code"] != 0 for check in checks):
            record.status = "failed"
            record.error = "deterministic_verification_failed"
        else:
            record.status = "approval_pending"
            record.approval_required = True
            record.approval_status = "pending_user"
        record.updated_at = _now()
        self.store.save(record)
        return record

    def status(self, run_id: str) -> RepairRecord:
        return self.store.load(run_id)


__all__ = [
    "LlamaCppPatchProposer",
    "LocalModelToolCodeRetriever",
    "RepairManager",
    "RepairRecord",
    "RepairStore",
]
