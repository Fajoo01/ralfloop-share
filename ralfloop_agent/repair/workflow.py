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

from ralfloop_agent.inference_lab.gguf_tokenizer import GGUFError, GGUFTokenizer
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

    def create(self, record: RepairRecord) -> None:
        """Atomically persist a new run without replacing an existing ID."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.root / f"{record.run_id}.json"
        temporary = self.root / f".{record.run_id}.{uuid4().hex}.tmp"
        temporary.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise ValueError("repair_run_id_exists") from exc
        finally:
            temporary.unlink(missing_ok=True)

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
    """LLM proposer using deterministic line-coordinate edit protocol v2.

    The model never copies source anchors and never writes unified-diff syntax.
    It selects visible 1-based half-open line ranges and returns replacement
    source as raw text. Python validates and applies the ranges in memory and
    builds the Git patch deterministically.
    """

    _EDIT_HEADER = re.compile(
        r'^@@RALF_EDIT[ \t]+path=(?P<path>"[^"\r\n]+"|[^"\s]+)'
        r'[ \t]+start=(?P<start>"[0-9]+"|[0-9]+)'
        r'[ \t]+end=(?P<end>"[0-9]+"|[0-9]+)[ \t]*$'
    )

    _SYSTEM_MESSAGE = (
        "You are a precise source-code editor. "
        "Output only RALF_EDIT protocol v2 blocks. "
        "Never output JSON, Markdown, prose or Git diff."
    )

    _CORRECTION_MARKER = (
        "\n\nCORRECTION ATTEMPT 2 OF 2."
    )

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        scheduler: TransactionalGpuScheduler | None = None,
    ) -> None:
        base = LlamaCppServerConfig.from_env()

        repair_context = int(
            os.getenv(
                "RALF_REPAIR_LLAMA_CONTEXT",
                str(base.context),
            )
        )

        if repair_context <= 0:
            raise ValueError("repair_context_must_be_positive")

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
            float(
                os.getenv(
                    "RALF_REPAIR_LLAMA_TIMEOUT_SEC",
                    "900",
                )
            ),
        )

        self.max_output_tokens = int(
            os.getenv(
                "RALF_REPAIR_MAX_OUTPUT_TOKENS",
                "1200",
            )
        )

        self.max_source_chars = int(
            os.getenv(
                "RALF_REPAIR_MAX_SOURCE_CHARS",
                "12000",
            )
        )

        self.context_safety_margin = max(
            128,
            int(
                os.getenv(
                    "RALF_REPAIR_CONTEXT_SAFETY_MARGIN",
                    "512",
                )
            ),
        )

        if self.max_output_tokens <= 0:
            raise ValueError(
                "repair_max_output_tokens_must_be_positive"
            )

        if self.max_source_chars <= 0:
            raise ValueError(
                "repair_max_source_chars_must_be_positive"
            )

        if (
            self.max_output_tokens
            + self.context_safety_margin
            >= self.config.context
        ):
            raise ValueError(
                "repair_reserved_context_exceeds_context"
            )

        self._tokenizer: GGUFTokenizer | None = None
        self._tokenizer_checked = False

    @staticmethod
    def _description_tokens(description: str) -> list[str]:
        tokens = set(
            re.findall(
                r"[A-Za-z_][A-Za-z0-9_]{3,}",
                description,
            )
        )
        return sorted(
            tokens,
            key=lambda value: (
                -int("_" in value),
                -len(value),
                value,
            ),
        )[:80]

    @classmethod
    def _source_selection_description(
        cls,
        description: str,
    ) -> str:
        # Verification output contains coordinates from the discarded
        # candidate.  It must guide the correction, but must not move the
        # ORIGINAL-source windows between bounded attempts.
        return description.partition(
            cls._CORRECTION_MARKER
        )[0]

    @classmethod
    def _source_windows(
        cls,
        text: str,
        description: str,
    ) -> list[tuple[int, int]]:
        lines = text.splitlines(keepends=True)
        total = len(lines)

        if total == 0:
            return [(1, 1)]

        tokens = cls._description_tokens(description)
        scored: list[tuple[int, int]] = []

        for line_no, line in enumerate(lines, 1):
            score = 0
            for token in tokens:
                if token in line:
                    score += 12 if "_" in token else min(len(token), 8)
            if score:
                scored.append((score, line_no))

        scored.sort(key=lambda row: (-row[0], row[1]))

        windows: list[tuple[int, int]] = [
            (1, min(total, 60)),
        ]

        for _score, line_no in scored[:10]:
            windows.append(
                (
                    max(1, line_no - 100),
                    min(total, line_no + 100),
                )
            )

        if len(windows) == 1:
            windows.append((1, min(total, 260)))

        windows.sort()

        merged: list[list[int]] = []
        for start, end in windows:
            if not merged or start > merged[-1][1] + 1:
                merged.append([start, end])
            else:
                merged[-1][1] = max(merged[-1][1], end)

        return [(start, end) for start, end in merged]

    @classmethod
    def _render_source(
        cls,
        text: str,
        description: str,
        budget: int,
    ) -> tuple[str, list[tuple[int, int]]]:
        lines = text.splitlines(keepends=True)

        if not lines:
            return "000001|<EMPTY FILE>\n", [(1, 1)]

        rendered: list[str] = []
        visible: list[tuple[int, int]] = []
        used = 0

        for start, end in cls._source_windows(text, description):
            actual_start: int | None = None
            actual_end: int | None = None

            rendered.append(
                f'<lines start="{start}" end="{end}">\n'
            )

            for line_no in range(start, end + 1):
                line = lines[line_no - 1]
                row = f"{line_no:06d}|{line}"

                if used + len(row) > budget:
                    break

                if actual_start is None:
                    actual_start = line_no

                actual_end = line_no
                rendered.append(row)
                used += len(row)

            rendered.append("</lines>\n")

            if actual_start is not None and actual_end is not None:
                visible.append((actual_start, actual_end))

            if used >= budget:
                break

        return "".join(rendered), visible

    def _source_blocks(
        self,
        worktree: Path,
        selected_files: list[str],
        description: str = "",
        *,
        source_char_budget: int | None = None,
    ) -> tuple[
        dict[str, str],
        str,
        dict[str, list[tuple[int, int]]],
    ]:
        if not selected_files:
            raise RuntimeError("repair_no_selected_files")

        originals: dict[str, str] = {}

        for rel in selected_files:
            originals[rel] = (worktree / rel).read_text(
                encoding="utf-8"
            )

        mentioned = [
            rel
            for rel in selected_files
            if rel in description
        ]

        ordered = mentioned + [
            rel
            for rel in selected_files
            if rel not in mentioned
        ]

        weights = {
            rel: (4 if rel in mentioned else 1)
            for rel in ordered
        }

        total_limit = self.max_source_chars

        if source_char_budget is not None:
            total_limit = min(
                total_limit,
                max(0, int(source_char_budget)),
            )

        if total_limit <= 0:
            return (
                originals,
                "",
                {rel: [] for rel in selected_files},
            )

        blocks: list[str] = []
        visible_spans: dict[
            str,
            list[tuple[int, int]],
        ] = {
            rel: [] for rel in selected_files
        }

        remaining_chars = total_limit
        remaining_weight = max(
            1,
            sum(weights.values()),
        )

        for index, rel in enumerate(ordered):
            separator_cost = 2 if blocks else 0
            available = remaining_chars - separator_cost

            if available <= 0:
                break

            weight = weights[rel]
            is_last = index == len(ordered) - 1

            share = (
                available
                if is_last
                else int(
                    available
                    * weight
                    / remaining_weight
                )
            )

            remaining_weight = max(
                1,
                remaining_weight - weight,
            )

            if share <= 0:
                continue

            prefix = f'<file path="{rel}">\n'
            suffix = "</file>"
            wrapper_cost = len(prefix) + len(suffix)

            if share <= wrapper_cost:
                continue

            body_budget = share - wrapper_cost

            body, spans = self._render_source(
                originals[rel],
                description,
                body_budget,
            )

            block = prefix + body + suffix

            while (
                len(block) > share
                and body_budget > 0
            ):
                excess = len(block) - share
                body_budget = max(
                    0,
                    body_budget - max(1, excess),
                )

                body, spans = self._render_source(
                    originals[rel],
                    description,
                    body_budget,
                )

                block = prefix + body + suffix

            if len(block) > share:
                continue

            blocks.append(block)
            visible_spans[rel] = spans
            remaining_chars -= (
                separator_cost + len(block)
            )

            if remaining_chars <= 0:
                break

        sources = "\n\n".join(blocks)

        if len(sources) > total_limit:
            raise RuntimeError(
                "repair_source_budget_internal_error"
            )

        return originals, sources, visible_spans

    def _get_tokenizer(
        self,
    ) -> GGUFTokenizer | None:
        if self._tokenizer_checked:
            return self._tokenizer

        self._tokenizer_checked = True

        try:
            model_path = Path(self.config.model_path)

            if not model_path.is_file():
                return None

            self._tokenizer = GGUFTokenizer.from_file(
                model_path
            )

        except (OSError, ValueError, GGUFError):
            self._tokenizer = None

        return self._tokenizer

    def _count_input_tokens(
        self,
        prompt: str,
    ) -> int:
        text = (
            self._SYSTEM_MESSAGE
            + "\n"
            + prompt
        )

        tokenizer = self._get_tokenizer()

        if tokenizer is not None:
            try:
                return len(tokenizer.encode(text))
            except GGUFError:
                self._tokenizer = None

        # Fallback fail-closed: volutamente conservativo.
        return len(text.encode("utf-8"))

    def _context_requirement(
        self,
        prompt: str,
    ) -> tuple[int, int]:
        input_tokens = self._count_input_tokens(
            prompt
        )

        required = (
            input_tokens
            + self.max_output_tokens
            + self.context_safety_margin
        )

        return input_tokens, required

    @staticmethod
    def _build_prompt(
        description: str,
        sources: str,
    ) -> str:
        return (
            "Implement the requested repair using "
            "RALF_EDIT protocol v2.\n\n"
            "Each visible source line is prefixed with "
            "its ORIGINAL 1-based line number and '|'. "
            "The prefix is NOT source code.\n\n"
            "For every edit output exactly:\n"
            "@@RALF_EDIT path=RELATIVE_PATH "
            "start=START end=END\n"
            "<raw replacement source; no line-number "
            "prefixes>\n"
            "@@RALF_END\n\n"
            "Coordinates are 1-based and half-open "
            "against the ORIGINAL file. "
            "For insertion use start=end.\n\n"
            "Rules:\n"
            "- Never copy old source text as an anchor.\n"
            "- Never output JSON, Markdown, prose or "
            "unified diff.\n"
            "- Edit only lines visible in the supplied "
            "windows.\n"
            "- Modify only supplied files.\n"
            "- Keep changes minimal and bounded.\n"
            "- Do not weaken tests.\n\n"
            f"Problem:\n{description[:8000]}\n\n"
            f"Sources:\n{sources}"
        )

    def _request_plan(
        self,
        prompt: str,
        selected_files: list[str],
    ) -> str:
        input_tokens, required = (
            self._context_requirement(prompt)
        )

        if required > int(self.config.context):
            raise RuntimeError(
                "repair_context_budget_exceeded:"
                f"input={input_tokens}:"
                f"output={self.max_output_tokens}:"
                f"margin={self.context_safety_margin}:"
                f"context={self.config.context}"
            )

        payload = {
            "model": self.config.model,
            "temperature": 0,
            "max_tokens": self.max_output_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": self._SYSTEM_MESSAGE,
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
        }

        response = self.session.post(
            f"{self.config.base_url}"
            "/v1/chat/completions",
            json=payload,
            timeout=(
                2.0,
                self.request_timeout_sec,
            ),
        )

        try:
            response.raise_for_status()
            envelope = response.json()
        finally:
            response.close()

        try:
            content = envelope[
                "choices"
            ][0]["message"]["content"]
        except (
            KeyError,
            IndexError,
            TypeError,
        ) as exc:
            raise RuntimeError(
                "repair_edit_response_invalid"
            ) from exc

        if not isinstance(content, str):
            raise RuntimeError(
                "repair_edit_content_not_text"
            )

        return content

    @classmethod
    def _parse_edit_protocol(
        cls,
        content: str,
        selected_files: list[str],
    ) -> list[dict[str, Any]]:
        text = content.strip()

        if text.startswith("```") and text.endswith("```"):
            rows = text.splitlines(keepends=True)
            if len(rows) >= 2:
                rows = rows[1:-1]
                text = "".join(rows).strip()

        rows = text.splitlines(keepends=True)
        edits: list[dict[str, Any]] = []
        index = 0

        while index < len(rows):
            raw = rows[index]
            stripped = raw.rstrip("\r\n")

            if not stripped.strip():
                index += 1
                continue

            match = cls._EDIT_HEADER.fullmatch(stripped)

            if match is None:
                raise RuntimeError(
                    f"repair_edit_unexpected_text:{index + 1}"
                )

            rel = match.group("path")
            start_raw = match.group("start")
            end_raw = match.group("end")

            if rel.startswith('"'):
                rel = rel[1:-1]
            if start_raw.startswith('"'):
                start_raw = start_raw[1:-1]
            if end_raw.startswith('"'):
                end_raw = end_raw[1:-1]

            start = int(start_raw)
            end = int(end_raw)

            if rel not in selected_files:
                raise RuntimeError(
                    f"repair_structured_path_forbidden:{rel}"
                )

            index += 1
            body: list[str] = []

            while index < len(rows):
                if rows[index].strip() == "@@RALF_END":
                    break

                body.append(rows[index])
                index += 1

            if index >= len(rows):
                raise RuntimeError(
                    f"repair_edit_missing_end:{rel}"
                )

            edits.append(
                {
                    "path": rel,
                    "start": start,
                    "end": end,
                    "replacement": "".join(body),
                }
            )

            if len(edits) > 24:
                raise RuntimeError("repair_structured_edit_limit")

            index += 1

        if not edits:
            raise RuntimeError("repair_structured_edits_missing")

        return edits

    @staticmethod
    def _range_is_visible(
        spans: list[tuple[int, int]],
        start: int,
        end: int,
    ) -> bool:
        if start == end:
            return any(
                span_start <= start <= span_end + 1
                for span_start, span_end in spans
            )

        return any(
            span_start <= start
            and end - 1 <= span_end
            for span_start, span_end in spans
        )

    @classmethod
    def _apply_plan(
        cls,
        originals: dict[str, str],
        selected_files: list[str],
        plan: dict[str, Any],
        *,
        visible_spans: dict[
            str,
            list[tuple[int, int]],
        ] | None = None,
    ) -> dict[str, str]:
        edits = plan.get("edits")

        if not isinstance(edits, list) or not edits:
            raise RuntimeError("repair_structured_edits_missing")

        if len(edits) > 24:
            raise RuntimeError("repair_structured_edit_limit")

        normalized: dict[
            str,
            list[tuple[int, int, str, int]],
        ] = {}

        for index, edit in enumerate(edits):
            if not isinstance(edit, dict):
                raise RuntimeError(
                    f"repair_structured_edit_not_object:{index}"
                )

            rel = edit.get("path")
            start = edit.get("start")
            end = edit.get("end")
            replacement = edit.get("replacement")

            if rel not in selected_files:
                raise RuntimeError(
                    f"repair_structured_path_forbidden:{rel}"
                )

            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or not isinstance(end, int)
                or isinstance(end, bool)
            ):
                raise RuntimeError(
                    f"repair_edit_range_invalid:{rel}:{index}"
                )

            if not isinstance(replacement, str):
                raise RuntimeError(
                    f"repair_structured_new_invalid:{rel}:{index}"
                )

            line_count = len(
                originals[rel].splitlines(keepends=True)
            )

            if (
                start < 1
                or end < start
                or end > line_count + 1
            ):
                raise RuntimeError(
                    f"repair_edit_range_bounds:"
                    f"{rel}:{index}:{start}:{end}:{line_count}"
                )

            if visible_spans is not None:
                spans = visible_spans.get(rel, [])

                if not cls._range_is_visible(
                    spans,
                    start,
                    end,
                ):
                    raise RuntimeError(
                        f"repair_edit_range_not_visible:"
                        f"{rel}:{index}:{start}:{end}"
                    )

            normalized.setdefault(rel, []).append(
                (
                    start - 1,
                    end - 1,
                    replacement,
                    index,
                )
            )

        modified = dict(originals)

        for rel, file_edits in normalized.items():
            ordered = sorted(
                file_edits,
                key=lambda row: (row[0], row[1], row[3]),
            )

            previous_end = -1
            previous_start = -1

            for start0, end0, _replacement, index in ordered:
                if start0 < previous_end:
                    raise RuntimeError(
                        f"repair_edit_overlap:{rel}:{index}"
                    )

                if start0 == previous_start:
                    raise RuntimeError(
                        f"repair_edit_same_coordinate:{rel}:{index}"
                    )

                previous_start = start0
                previous_end = max(previous_end, end0)

            lines = originals[rel].splitlines(
                keepends=True
            )

            for start0, end0, replacement, _index in sorted(
                file_edits,
                key=lambda row: (row[0], row[1], row[3]),
                reverse=True,
            ):
                replacement_lines = replacement.splitlines(
                    keepends=True
                )

                lines[start0:end0] = replacement_lines

            modified[rel] = "".join(lines)

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

            current = (worktree / rel).read_text(
                encoding="utf-8"
            )

            if current != before:
                raise RuntimeError(
                    f"repair_source_changed_during_proposal:{rel}"
                )

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

        source_budget = self.max_source_chars
        source_description = (
            self._source_selection_description(
                description
            )
        )

        while True:
            (
                originals,
                sources,
                visible_spans,
            ) = self._source_blocks(
                worktree,
                selected_files,
                source_description,
                source_char_budget=source_budget,
            )

            base_prompt = self._build_prompt(
                description,
                sources,
            )

            input_tokens, required = (
                self._context_requirement(
                    base_prompt
                )
            )

            if required <= int(
                self.config.context
            ):
                break

            if source_budget <= 0:
                raise RuntimeError(
                    "repair_context_budget_exceeded:"
                    f"input={input_tokens}:"
                    f"output={self.max_output_tokens}:"
                    f"margin="
                    f"{self.context_safety_margin}:"
                    f"context={self.config.context}"
                )

            next_budget = (
                source_budget * 3
            ) // 4

            if next_budget >= source_budget:
                next_budget = source_budget - 1

            source_budget = max(
                0,
                next_budget,
            )

        if not sources:
            raise RuntimeError(
                "repair_context_budget_exceeded:"
                "no_source_capacity"
            )

        with self.scheduler.engine_session(
            "qwen_chat",
            task_id="repair_patch_proposal",
        ):
            raw = self._request_plan(
                base_prompt,
                selected_files,
            )

            plan = {
                "edits": self._parse_edit_protocol(
                    raw,
                    selected_files,
                )
            }

            modified = self._apply_plan(
                originals,
                selected_files,
                plan,
                visible_spans=visible_spans,
            )

            return self._build_patch(
                worktree,
                originals,
                modified,
                selected_files,
            )

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

    def _new_record(
        self,
        action: str,
        description: str,
        *,
        run_id: str | None = None,
    ) -> RepairRecord:
        selected_run_id = run_id or uuid4().hex
        if not re.fullmatch(
            r"[a-f0-9]{32}",
            selected_run_id,
        ):
            raise ValueError("invalid_repair_run_id")
        now = _now()
        return RepairRecord(
            run_id=selected_run_id,
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
        self.store.create(record)
        return record

    def _tests(self, worktree: Path) -> list[list[str]]:
        tests = list((worktree / "tests").glob("test_*.py")) if (worktree / "tests").is_dir() else []
        if not tests or len(tests) > 50:
            return []
        return [[self.python_executable, "-m", "pytest", "-q"]]

    def _persist_stage(
        self,
        record: RepairRecord,
        status: str,
        *,
        create: bool = False,
    ) -> None:
        record.status = status
        record.updated_at = _now()
        if create:
            self.store.create(record)
        else:
            self.store.save(record)

    def run(
        self,
        description: str,
        *,
        run_id: str | None = None,
    ) -> RepairRecord:
        record = self._new_record(
            "run",
            description,
            run_id=run_id,
        )
        record.rollback = [
            "No worktree created; no rollback required."
        ]
        self._persist_stage(
            record,
            "source_preflight",
            create=True,
        )

        source_status = _command(
            [
                "git",
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            self.source_repo,
            timeout=20,
        )
        source_preflight: dict[str, Any] = {
            "clean": (
                source_status["exit_code"] == 0
                and not str(source_status["stdout"]).strip()
            ),
            "status": source_status,
        }
        record.validation = {
            "source_preflight": source_preflight,
        }

        if source_status["exit_code"] != 0:
            record.status = "failed"
            record.error = "repair_source_status_failed"
            record.updated_at = _now()
            self.store.save(record)
            return record

        if str(source_status["stdout"]).strip():
            record.status = "blocked"
            record.error = "repair_source_dirty"
            record.updated_at = _now()
            self.store.save(record)
            return record

        source_head = _command(
            [
                "git",
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
            ],
            self.source_repo,
            timeout=10,
        )
        source_preflight["head"] = source_head

        if source_head["exit_code"] != 0:
            record.status = "failed"
            record.error = "repair_source_head_failed"
            record.updated_at = _now()
            self.store.save(record)
            return record

        base_head = str(source_head["stdout"]).strip()
        run_root = self.state_root / "runs" / record.run_id
        worktree = run_root / "worktree"
        run_root.mkdir(parents=True, exist_ok=False, mode=0o700)
        record.worktree = str(worktree)
        record.rollback = [
            f"git -C {self.source_repo} worktree remove {worktree}",
            f"Review and then remove runtime record {self.store.root / (record.run_id + '.json')}",
        ]
        self._persist_stage(record, "creating_worktree")

        added = _command(
            [
                "git",
                "worktree",
                "add",
                "--detach",
                str(worktree),
                base_head,
            ],
            self.source_repo,
            timeout=60,
        )
        if added["exit_code"] != 0:
            record.status = "failed"
            record.error = "isolated_worktree_creation_failed"
            record.validation = {
                "source_preflight": source_preflight,
                "worktree_add": added,
            }
            record.updated_at = _now()
            self.store.save(record)
            return record

        self._persist_stage(record, "diagnosing")
        test_commands = self._tests(worktree)
        record.pre_tests = [
            _command(command, worktree)
            for command in test_commands
        ]

        retrieval = self.code_retriever(
            worktree,
            description,
        )
        record.code_retriever = retrieval
        record.selected_files = [
            str(path)
            for path in retrieval.get("files", [])[
                : self.validator.max_files
            ]
        ]

        if (
            not retrieval.get("ok")
            or not record.selected_files
        ):
            record.status = "blocked"
            record.error = str(
                retrieval.get("error_type")
                or "code_retriever_unavailable"
            )
            record.updated_at = _now()
            self.store.save(record)
            return record

        bounded_validator = PatchValidator(
            max_files=self.validator.max_files,
            max_changed_lines=self.validator.max_changed_lines,
            allowed_paths=tuple(record.selected_files),
            allowed_suffixes=self.validator.allowed_suffixes,
        )

        attempts: list[dict[str, Any]] = []
        correction_evidence = ""

        for attempt_index in range(2):
            attempt_no = attempt_index + 1

            record.validation = {
                "source_preflight": source_preflight,
                "attempts": attempts,
            }
            self._persist_stage(record, "proposing")

            proposal_description = description
            if correction_evidence:
                proposal_description = (
                    description
                    + LlamaCppPatchProposer._CORRECTION_MARKER
                    + " "
                    "The previous candidate failed deterministic "
                    "verification. Produce a corrected candidate against "
                    "the ORIGINAL source. Do not change the requested "
                    "behavior. Failure evidence may contain line numbers "
                    "from the discarded candidate; select coordinates "
                    "only from the supplied ORIGINAL source windows."
                    "\n\nFailure evidence:\n"
                    + correction_evidence[:6000]
                )

            try:
                patch = self.proposer(
                    worktree,
                    proposal_description,
                    record.selected_files,
                )
            except Exception as exc:
                evidence = (
                    "patch_proposal_failed:"
                    f"{type(exc).__name__}:{exc}"
                )
                attempts.append(
                    {
                        "attempt": attempt_no,
                        "stage": "proposal",
                        "ok": False,
                        "error": evidence,
                    }
                )

                if attempt_index == 0:
                    correction_evidence = evidence
                    continue

                record.status = "failed"
                record.error = evidence
                record.validation = {
                    "source_preflight": source_preflight,
                    "attempts": attempts,
                }
                record.updated_at = _now()
                self.store.save(record)
                return record

            validation = bounded_validator.validate(
                patch,
                worktree,
            )
            validation_payload = {
                "ok": validation.ok,
                "files": list(validation.files),
                "changed_lines": validation.changed_lines,
                "errors": list(validation.errors),
            }

            if not validation.ok:
                evidence = (
                    "patch_validation_failed:"
                    + ";".join(validation.errors)
                )
                attempts.append(
                    {
                        "attempt": attempt_no,
                        "stage": "validation",
                        "ok": False,
                        "error": evidence,
                        "validation": validation_payload,
                    }
                )
                record.validation = {
                    "source_preflight": source_preflight,
                    **validation_payload,
                    "attempts": attempts,
                }

                if attempt_index == 0:
                    correction_evidence = evidence
                    continue

                record.status = "failed"
                record.error = "patch_validation_failed"
                record.updated_at = _now()
                self.store.save(record)
                return record

            applied = _command(
                ["git", "apply", "-"],
                worktree,
                timeout=20,
                input_text=patch,
            )

            if applied["exit_code"] != 0:
                evidence = (
                    "patch_apply_failed:\n"
                    + str(applied.get("stderr") or "")[:3000]
                )
                attempts.append(
                    {
                        "attempt": attempt_no,
                        "stage": "apply",
                        "ok": False,
                        "error": evidence,
                        "apply": applied,
                    }
                )
                record.validation = {
                    "source_preflight": source_preflight,
                    **validation_payload,
                    "apply": applied,
                    "attempts": attempts,
                }

                if attempt_index == 0:
                    correction_evidence = evidence
                    continue

                record.status = "failed"
                record.error = "patch_apply_failed"
                record.updated_at = _now()
                self.store.save(record)
                return record

            changed = [
                path
                for path in validation.files
                if path.endswith(".py")
            ]

            record.validation = {
                "source_preflight": source_preflight,
                **validation_payload,
                "attempts": attempts,
            }
            self._persist_stage(record, "verifying")

            checks: list[dict[str, Any]] = []

            if changed:
                checks.append(
                    _command(
                        [
                            self.python_executable,
                            "-m",
                            "py_compile",
                            *changed,
                        ],
                        worktree,
                        timeout=60,
                        env_overrides={
                            "PYTHONPYCACHEPREFIX":
                                str(run_root / "pycache"),
                        },
                    )
                )

            checks.extend(
                _command(command, worktree)
                for command in test_commands
            )

            checks.append(
                _command(
                    ["git", "diff", "--check"],
                    worktree,
                    timeout=20,
                )
            )

            record.post_tests = checks
            record.diff = patch

            failing = [
                check
                for check in checks
                if check["exit_code"] != 0
            ]

            if not failing:
                attempts.append(
                    {
                        "attempt": attempt_no,
                        "stage": "verification",
                        "ok": True,
                        "checks": checks,
                    }
                )
                record.validation = {
                    "source_preflight": source_preflight,
                    **validation_payload,
                    "attempts": attempts,
                }
                record.status = "approval_pending"
                record.approval_required = True
                record.approval_status = "pending_user"
                record.error = None
                record.updated_at = _now()
                self.store.save(record)
                return record

            evidence_rows = [
                "deterministic_verification_failed"
            ]

            for check in failing:
                evidence_rows.append(
                    "\ncommand: "
                    + str(check.get("command") or "")
                    + "\nexit_code: "
                    + str(check.get("exit_code"))
                    + "\nstdout:\n"
                    + str(check.get("stdout") or "")[:1500]
                    + "\nstderr:\n"
                    + str(check.get("stderr") or "")[:2500]
                )

            evidence = "\n".join(evidence_rows)

            attempt_row: dict[str, Any] = {
                "attempt": attempt_no,
                "stage": "verification",
                "ok": False,
                "error": "deterministic_verification_failed",
                "checks": checks,
            }
            attempts.append(attempt_row)

            record.validation = {
                "source_preflight": source_preflight,
                **validation_payload,
                "attempts": attempts,
            }

            if attempt_index == 0:
                reverted = _command(
                    ["git", "apply", "-R", "-"],
                    worktree,
                    timeout=20,
                    input_text=patch,
                )
                attempt_row["candidate_revert"] = reverted

                if reverted["exit_code"] != 0:
                    record.status = "failed"
                    record.error = "candidate_revert_failed"
                    record.updated_at = _now()
                    self.store.save(record)
                    return record

                clean = _command(
                    [
                        "git",
                        "diff",
                        "--quiet",
                        "--",
                        *validation.files,
                    ],
                    worktree,
                    timeout=20,
                )
                attempt_row["candidate_revert_clean"] = clean

                if clean["exit_code"] != 0:
                    record.status = "failed"
                    record.error = "candidate_revert_not_clean"
                    record.updated_at = _now()
                    self.store.save(record)
                    return record

                correction_evidence = evidence
                continue

            record.status = "failed"
            record.error = "deterministic_verification_failed"
            record.updated_at = _now()
            self.store.save(record)
            return record

        record.status = "failed"
        record.error = "repair_attempt_budget_exhausted"
        record.validation = {
            "source_preflight": source_preflight,
            "attempts": attempts,
        }
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
