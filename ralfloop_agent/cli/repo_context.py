from __future__ import annotations

import json
import os
import re
import selectors
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

DEFAULT_MAX_BYTES = 24_000
DEFAULT_MAX_FILES = 4
DEFAULT_MAX_COMMITS = 5
MAX_GIT_LINES = 40
MAX_GIT_METADATA_ENTRIES = 4_096
MAX_GIT_OUTPUT_BYTES = 64 * 1024
MAX_REPO_CONTEXT_BYTES = 60 * 1024

EXCLUDED_PARTS = {
    ".git",
    ".openshell_backend",
    ".ralf_run",
    ".sandbox",
    ".venv",
    "__pycache__",
    "logs",
    "node_modules",
    "runtime",
    "venv",
}
SENSITIVE_MARKERS = (".env", "credential", "password", "secret", "token")
CONTEXT_FILENAMES = ("AGENTS.md", "README.md", "README.rst", "README")


class RepoContextError(ValueError):
    pass


def resolve_cwd(value: str | os.PathLike[str]) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise RepoContextError("invalid_cwd") from exc
    if not path.is_dir():
        raise RepoContextError("invalid_cwd")
    return path


def collect_repo_context(
    cwd: str | os.PathLike[str],
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_files: int = DEFAULT_MAX_FILES,
    max_commits: int = DEFAULT_MAX_COMMITS,
) -> dict[str, Any]:
    root = resolve_cwd(cwd)
    byte_budget = max(0, int(max_bytes))
    file_budget = max(0, int(max_files))
    context: dict[str, Any] = {
        "cwd": str(root),
        "repository": root.name,
        "branch": _local_git_branch(root),
        "git_metadata_limited": False,
        "git_status": [],
        "recent_commits": [],
        "files": [],
        "truncated": False,
    }

    has_local_git = _local_git_directory(root) is not None
    safe_git_metadata = _has_local_git_metadata(root)
    context["git_metadata_limited"] = has_local_git and not safe_git_metadata
    if safe_git_metadata:
        context["branch"] = _git(root, "rev-parse", "--abbrev-ref", "HEAD") or context["branch"]
        exclusions = [
            f":(exclude,glob)**/{part}/**"
            for part in sorted(EXCLUDED_PARTS - {".git"})
        ]
        status = _git(
            root,
            "status",
            "--short",
            "--untracked-files=normal",
            "--ignore-submodules=all",
            "--",
            ".",
            *exclusions,
        )
        context["git_status"] = _safe_git_lines(status, MAX_GIT_LINES)
        commits = _git(root, "log", f"-{max(0, int(max_commits))}", "--pretty=format:%h %s")
        context["recent_commits"] = _safe_git_lines(commits, max(0, int(max_commits)))

    used_bytes = 0
    for name in CONTEXT_FILENAMES:
        if len(context["files"]) >= file_budget or used_bytes >= byte_budget:
            context["truncated"] = True
            break
        candidate = root / name
        safe_path = _safe_context_file(root, candidate)
        if safe_path is None:
            continue
        remaining = byte_budget - used_bytes
        content, was_truncated = _read_bounded(safe_path, remaining)
        used_bytes += len(content.encode("utf-8"))
        context["files"].append(
            {
                "path": safe_path.relative_to(root).as_posix(),
                "content": content,
                "truncated": was_truncated,
            }
        )
        context["truncated"] = context["truncated"] or was_truncated

    context["bytes"] = used_bytes
    _fit_serialized_context(context, MAX_REPO_CONTEXT_BYTES)
    return context


def _git(root: Path, *args: str) -> str:
    git_binary = shutil.which("git", path=os.defpath)
    if not git_binary:
        return ""
    env = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "PATH": os.defpath,
    }
    command = [
        git_binary,
        "-c",
        "color.ui=false",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.excludesFile=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "diff.external=",
        "-C",
        str(root),
        *args,
    ]
    return _run_bounded(command, env=env, timeout=2, max_bytes=MAX_GIT_OUTPUT_BYTES)


def _run_bounded(command: list[str], *, env: dict[str, str], timeout: float, max_bytes: int) -> str:
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    except OSError:
        return ""
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    deadline = time.monotonic() + timeout
    truncated = False
    try:
        while selector.get_map():
            remaining = max_bytes + 1 - len(output)
            if remaining <= 0:
                truncated = True
                break
            wait = deadline - time.monotonic()
            if wait <= 0:
                break
            events = selector.select(timeout=min(wait, 0.1))
            if not events:
                continue
            for key, _ in events:
                chunk = os.read(key.fileobj.fileno(), min(8_192, remaining))
                if chunk:
                    output.extend(chunk)
                else:
                    selector.unregister(key.fileobj)
        if len(output) > max_bytes:
            truncated = True
            del output[max_bytes:]
    finally:
        selector.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        process.stdout.close()
    if process.returncode != 0 and not truncated:
        return ""
    return output.decode("utf-8", errors="replace").strip()


def _has_local_git_metadata(root: Path) -> bool:
    git_dir = _local_git_directory(root)
    if git_dir is None:
        return False

    entries = 0
    pending = [git_dir]
    try:
        while pending:
            current = pending.pop()
            with os.scandir(current) as children:
                for child in children:
                    entries += 1
                    if entries > MAX_GIT_METADATA_ENTRIES or child.is_symlink():
                        return False
                    if child.is_dir(follow_symlinks=False):
                        pending.append(Path(child.path))
        config = git_dir / "config"
        if config.is_file():
            with config.open("rb") as handle:
                config_text = handle.read(128_001)
            if len(config_text) > 128_000:
                return False
            decoded = config_text.decode("utf-8", errors="replace")
            if re.search(r"(?im)^\s*\[include(?:if)?\b", decoded):
                return False
            if re.search(r"(?im)^\s*worktree\s*=", decoded):
                return False
        worktree_config = git_dir / "config.worktree"
        if worktree_config.is_file() and worktree_config.stat().st_size:
            return False
        for indirect_path in (git_dir / "commondir", git_dir / "objects" / "info" / "alternates"):
            if indirect_path.is_file() and indirect_path.stat().st_size:
                return False
    except OSError:
        return False
    return True


def _local_git_directory(root: Path) -> Path | None:
    git_dir = root / ".git"
    if not git_dir.is_dir() or git_dir.is_symlink():
        return None
    try:
        git_dir.resolve(strict=True).relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return git_dir


def _local_git_branch(root: Path) -> str | None:
    git_dir = _local_git_directory(root)
    if git_dir is None:
        return None
    head = git_dir / "HEAD"
    if not head.is_file() or head.is_symlink():
        return None
    try:
        with head.open("rb") as handle:
            raw = handle.read(513)
    except OSError:
        return None
    if len(raw) > 512:
        return None
    value = raw.decode("ascii", errors="ignore").strip()
    if value.startswith("ref: refs/heads/"):
        branch = value.removeprefix("ref: refs/heads/")
        if re.fullmatch(r"[A-Za-z0-9._/-]{1,255}", branch) and ".." not in branch:
            return branch
    if re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        return "HEAD"
    return None


def _serialized_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _fit_serialized_context(context: dict[str, Any], limit: int) -> None:
    while _serialized_size(context) > limit:
        files = context.get("files")
        content_row = next(
            (
                row
                for row in reversed(files if isinstance(files, list) else [])
                if isinstance(row, dict) and row.get("content")
            ),
            None,
        )
        if content_row is not None:
            content = str(content_row["content"])
            content_row["content"] = content[: len(content) // 2]
            content_row["truncated"] = True
            context["truncated"] = True
            context["bytes"] = sum(
                len(str(row.get("content") or "").encode("utf-8"))
                for row in files
                if isinstance(row, dict)
            )
            continue
        status = context.get("git_status")
        if isinstance(status, list) and status:
            status.pop()
            context["truncated"] = True
            continue
        commits = context.get("recent_commits")
        if isinstance(commits, list) and commits:
            commits.pop()
            context["truncated"] = True
            continue
        break


def _safe_git_lines(text: str, limit: int) -> list[str]:
    rows: list[str] = []
    for row in text.splitlines():
        if _looks_sensitive(row):
            continue
        rows.append(row[:500])
        if len(rows) >= limit:
            break
    return rows


def _safe_context_file(root: Path, candidate: Path) -> Path | None:
    if not candidate.is_file() or candidate.is_symlink() or _looks_sensitive(candidate.name):
        return None
    try:
        resolved = candidate.resolve(strict=True)
        relative = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    if any(part in EXCLUDED_PARTS or _looks_sensitive(part) for part in relative.parts):
        return None
    return resolved


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in SENSITIVE_MARKERS)


def _read_bounded(path: Path, limit: int) -> tuple[str, bool]:
    if limit <= 0:
        return "", True
    with path.open("rb") as handle:
        raw = handle.read(limit + 1)
    truncated = len(raw) > limit
    return raw[:limit].decode("utf-8", errors="replace"), truncated
