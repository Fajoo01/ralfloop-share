from __future__ import annotations
from dataclasses import replace

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
from ralfloop_agent.providers.gpu_engine_scheduler import TransactionalGpuScheduler
from ralfloop_agent.providers.llama_cpp_server import LlamaCppServerConfig, LlamaCppServerManager

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
    """LLM code proposer with deterministic Git patch construction.

    The model never writes unified-diff syntax. It returns structured exact
    textual edits; Python applies those edits in memory and generates the Git
    patch deterministically.
    """

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        scheduler: TransactionalGpuScheduler | None = None,
    ) -> None:
        base = LlamaCppServerConfig.from_env()

        # Repair is deliberately llama.cpp-only and gets enough context for
        # source editing. The RTX lifecycle scheduler still decides when the
        # model may occupy the GPU.
        repair_context = max(
            int(base.context),
            int(os.getenv("RALF_REPAIR_LLAMA_CONTEXT", "32768")),
        )

        self.config = replace(
            base,
            fallback="none",
            context=repair_context,
        )

        self.session = session or requests.Session()

        self.scheduler = scheduler or TransactionalGpuScheduler(
            chat=LlamaCppServerManager(self.config)
        )

        self.request_timeout_sec = max(
            float(self.config.request_timeout_sec),
            float(os.getenv("RALF_REPAIR_LLAMA_TIMEOUT_SEC", "900")),
        )

        self.max_output_tokens = int(
            os.getenv("RALF_REPAIR_MAX_OUTPUT_TOKENS", "5000")
        )

        self.max_source_chars = int(
            os.getenv("RALF_REPAIR_MAX_SOURCE_CHARS", "65000")
        )

    def _source_blocks(
        self,
        worktree: Path,
        selected_files: list[str],
    ) -> tuple[dict[str, str], str]:
        originals: dict[str, str] = {}

        for rel in selected_files:
            path = worktree / rel
            originals[rel] = path.read_text(encoding="utf-8")

        if not selected_files:
            raise RuntimeError("repair_no_selected_files")

        per_file = max(
            4000,
            self.max_source_chars // len(selected_files),
        )

        blocks: list[str] = []

        for rel in selected_files:
            text = originals[rel]
            visible = text[:per_file]

            blocks.append(
                f'<file path="{rel}">\n'
                f"{visible}\n"
                f"</file>"
            )

        return originals, "\n\n".join(blocks)

    def _request_plan(
        self,
        prompt: str,
        selected_files: list[str],
    ) -> dict[str, Any]:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["edits"],
            "properties": {
                "edits": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 24,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["path", "kind", "old", "new"],
                        "properties": {
                            "path": {
                                "type": "string",
                                "enum": selected_files,
                            },
                            "kind": {
                                "type": "string",
                                "enum": ["replace", "append"],
                            },
                            "old": {
                                "type": "string",
                            },
                            "new": {
                                "type": "string",
                            },
                        },
                    },
                },
            },
        }

        base_payload = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a precise source-code editor. "
                        "Return only structured exact textual replacements. "
                        "Never return a Git diff."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        }

        payload = dict(base_payload)
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "ralf_repair_edits",
                "strict": True,
                "schema": schema,
            },
        }

        response = self.session.post(
            f"{self.config.base_url}/v1/chat/completions",
            json=payload,
            timeout=(2.0, self.request_timeout_sec),
        )

        # Compatibility path for llama.cpp builds that support JSON grammar
        # but not the newer json_schema form.
        if response.status_code == 400:
            response.close()

            payload = dict(base_payload)
            payload["response_format"] = {
                "type": "json_object"
            }

            response = self.session.post(
                f"{self.config.base_url}/v1/chat/completions",
                json=payload,
                timeout=(2.0, self.request_timeout_sec),
            )

        try:
            response.raise_for_status()
            envelope = response.json()
        finally:
            response.close()

        content = envelope["choices"][0]["message"]["content"]

        if isinstance(content, dict):
            plan = content
        else:
            try:
                plan = json.loads(str(content).strip())
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"repair_structured_json_invalid:{exc}"
                ) from exc

        if not isinstance(plan, dict):
            raise RuntimeError("repair_structured_plan_not_object")

        return plan

    @staticmethod
    def _apply_plan(
        originals: dict[str, str],
        selected_files: list[str],
        plan: dict[str, Any],
    ) -> dict[str, str]:
        edits = plan.get("edits")

        if not isinstance(edits, list) or not edits:
            raise RuntimeError("repair_structured_edits_missing")

        if len(edits) > 24:
            raise RuntimeError("repair_structured_edit_limit")

        modified = dict(originals)

        for index, edit in enumerate(edits):
            if not isinstance(edit, dict):
                raise RuntimeError(
                    f"repair_structured_edit_not_object:{index}"
                )

            rel = edit.get("path")
            kind = edit.get("kind", "replace")
            old = edit.get("old")
            new = edit.get("new")

            if rel not in selected_files:
                raise RuntimeError(
                    f"repair_structured_path_forbidden:{rel}"
                )

            if kind not in {"replace", "append"}:
                raise RuntimeError(
                    f"repair_structured_kind_invalid:{rel}:{index}:{kind}"
                )

            if not isinstance(old, str):
                raise RuntimeError(
                    f"repair_structured_old_invalid:{rel}:{index}"
                )

            if not isinstance(new, str):
                raise RuntimeError(
                    f"repair_structured_new_invalid:{rel}:{index}"
                )

            current = modified[rel]

            if kind == "append":
                # Append è intenzionalmente molto limitato:
                # può solo aggiungere in coda a un file già selezionato.
                # Non usa anchor inventate dal modello.
                if old != "":
                    raise RuntimeError(
                        f"repair_structured_append_old_must_be_empty:"
                        f"{rel}:{index}"
                    )

                if not new:
                    raise RuntimeError(
                        f"repair_structured_append_empty:{rel}:{index}"
                    )

                separator = ""

                if current and not current.endswith("\n"):
                    separator = "\n"

                modified[rel] = current + separator + new

            else:
                if not old:
                    raise RuntimeError(
                        f"repair_structured_old_invalid:{rel}:{index}"
                    )

                occurrences = current.count(old)

                if occurrences != 1:
                    raise RuntimeError(
                        "repair_structured_old_occurrences:"
                        f"{rel}:{index}:{occurrences}"
                    )

                modified[rel] = current.replace(
                    old,
                    new,
                    1,
                )

        return modified

    @staticmethod
    def _build_patch(
        worktree: Path,
        originals: dict[str, str],
        modified: dict[str, str],
        selected_files: list[str],
    ) -> str:
        import difflib
        import subprocess

        chunks: list[str] = []

        for rel in selected_files:
            before = originals[rel]
            after = modified[rel]

            if before == after:
                continue

            body = "".join(
                difflib.unified_diff(
                    before.splitlines(keepends=True),
                    after.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                    n=3,
                )
            )

            if body:
                chunks.append(
                    f"diff --git a/{rel} b/{rel}\n{body}"
                )

        patch = "".join(chunks)

        if not patch:
            raise RuntimeError("repair_structured_no_changes")

        if not patch.endswith("\n"):
            patch += "\n"

        check = subprocess.run(
            ["git", "apply", "--check", "-"],
            cwd=worktree,
            input=patch,
            text=True,
            capture_output=True,
            check=False,
        )

        if check.returncode != 0:
            raise RuntimeError(
                "repair_deterministic_patch_invalid:"
                + check.stderr.strip()[:1000]
            )

        return patch

    def __call__(
        self,
        worktree: Path,
        description: str,
        selected_files: list[str],
    ) -> str:
        selected_files = [
            str(path)
            for path in selected_files
        ]

        originals, sources = self._source_blocks(
            worktree,
            selected_files,
        )

        base_prompt = (
            "Implement the requested repair using exact textual edits.\n\n"
            "Rules:\n"
            "- DO NOT write a unified diff.\n"
            "- Use kind=replace when modifying existing text.\n"
            "- For replace, old must be copied exactly and contiguously "
            "from the provided source and must identify exactly one "
            "occurrence.\n"
            "- Use kind=append when adding new code at the END of a file.\n"
            "- For append, old MUST be the empty string.\n"
            "- Never invent an anchor merely to append content.\n"
            "- Modify only the supplied files.\n"
            "- Keep changes minimal and bounded.\n"
            "- Add or update tests when required.\n"
            "- Do not weaken existing tests merely to make them pass.\n"
            "- Do not modify approval policy, credentials, models, "
            "systemd, drivers or .git unless explicitly requested.\n\n"
            f"Problem:\n{description[:8000]}\n\n"
            f"Sources:\n{sources}"
        )

        # One GPU transaction may contain one bounded correction retry.
        with self.scheduler.engine_session(
            "qwen_chat",
            task_id="repair_patch_proposal",
        ):
            prompt = base_prompt
            last_error: RuntimeError | None = None

            for attempt in range(2):
                plan = self._request_plan(
                    prompt,
                    selected_files,
                )

                try:
                    modified = self._apply_plan(
                        originals,
                        selected_files,
                        plan,
                    )

                    return self._build_patch(
                        worktree,
                        originals,
                        modified,
                        selected_files,
                    )

                except RuntimeError as exc:
                    last_error = exc

                    if attempt:
                        raise

                    prompt = (
                        base_prompt
                        + "\n\nYour previous structured edit plan could "
                        "not be applied exactly. Produce a corrected plan. "
                        "Do not change the requested behavior.\n"
                        f"Validation error: {exc}"
                    )

            assert last_error is not None
            raise last_error

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
