from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re


MAX_LOG_FILES = 64
MAX_LOG_FILE_BYTES = 16 * 1024 * 1024
MAX_SEARCH_BYTES_PER_FILE = 512 * 1024
MAX_OPEN_LINES = 120
MAX_OPEN_CHARS = 32_000
_ALLOWED_SUFFIXES = {".log", ".jsonl"}

_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|passwd|secret|client[_-]?secret)\b(\s*[:=]\s*)([^\s,;\"']+)"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{12,}"),
)


@dataclass(frozen=True)
class SafeLogFile:
    log_id: str
    alias: str
    path: Path
    size_bytes: int
    modified_at: str


def _configured_roots() -> list[Path]:
    configured = os.getenv("RALF_MODEL_TOOL_LOG_ROOTS", "").strip()
    if configured:
        candidates = [
            Path(item).expanduser()
            for item in configured.split(os.pathsep)
            if item.strip()
        ]
    else:
        candidates = [
            Path.home() / ".local" / "state" / "ralf" / "model_tool_runs" / "deep_web_research",
            Path.home() / "ralfloop_agent_scaffold" / ".openshell_backend",
        ]
    roots: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_dir() and not resolved.is_symlink() and resolved not in roots:
            roots.append(resolved)
    return roots


def redact_log_text(text: str) -> str:
    redacted = str(text)
    redacted = _SECRET_PATTERNS[0].sub(lambda match: f"{match.group(1)}: [REDACTED]", redacted)
    redacted = _SECRET_PATTERNS[1].sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        redacted,
    )
    redacted = _SECRET_PATTERNS[2].sub("[REDACTED_TELEGRAM_TOKEN]", redacted)
    redacted = _SECRET_PATTERNS[3].sub("[REDACTED_JWT]", redacted)
    redacted = _SECRET_PATTERNS[4].sub(lambda match: f"{match.group(1)} [REDACTED]", redacted)
    return redacted


def discover_logs() -> dict[str, SafeLogFile]:
    candidates: list[tuple[float, str, Path, int]] = []
    for root_index, root in enumerate(_configured_roots(), start=1):
        for path in root.rglob("*"):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                resolved = path.resolve(strict=True)
                if not resolved.is_relative_to(root):
                    continue
                stat = resolved.stat()
            except OSError:
                continue
            if resolved.suffix.casefold() not in _ALLOWED_SUFFIXES:
                continue
            if stat.st_size < 0 or stat.st_size > MAX_LOG_FILE_BYTES:
                continue
            alias = f"root{root_index}/{resolved.relative_to(root).as_posix()}"
            candidates.append((stat.st_mtime, alias, resolved, stat.st_size))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    files: dict[str, SafeLogFile] = {}
    for index, (mtime, alias, path, size_bytes) in enumerate(candidates[:MAX_LOG_FILES], start=1):
        log_id = f"L{index}"
        files[log_id] = SafeLogFile(
            log_id=log_id,
            alias=alias,
            path=path,
            size_bytes=size_bytes,
            modified_at=datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
        )
    return files


def _tail_text(path: Path, max_bytes: int) -> str:
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - max_bytes))
        data = handle.read(max_bytes)
    text = data.decode("utf-8", "replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text


def search_logs(
    query: str,
    *,
    files: dict[str, SafeLogFile] | None = None,
    limit: int = 20,
    max_age_hours: int = 168,
) -> dict[str, object]:
    needle = str(query).strip().casefold()
    if not needle:
        raise ValueError("log_search_query_empty")
    terms = tuple(
        dict.fromkeys(
            token
            for token in re.findall(r"[a-z0-9_.:-]{3,}", needle)
            if token not in {"della", "delle", "degli", "errori", "errore", "relativi", "superamento"}
        )
    )
    limit = max(1, min(int(limit), 50))
    max_age_hours = max(1, min(int(max_age_hours), 24 * 366))
    now = datetime.now(timezone.utc).timestamp()
    catalog = files if files is not None else discover_logs()
    matches: list[dict[str, object]] = []
    for item in catalog.values():
        try:
            if now - item.path.stat().st_mtime > max_age_hours * 3600:
                continue
            text = redact_log_text(_tail_text(item.path, MAX_SEARCH_BYTES_PER_FILE))
        except OSError:
            continue
        lines = text.splitlines()
        for line_number, line in enumerate(lines, start=1):
            folded = line.casefold()
            exact = needle in folded
            matched_terms = [term for term in terms if term in folded]
            minimum_terms = 1 if len(terms) < 3 else 2
            if not exact and len(matched_terms) < minimum_terms:
                continue
            score = 1000 if exact else len(matched_terms)
            if "exceeds the available context size" in folded:
                score += 100
            if "send_error" in folded or "error:" in folded:
                score += 25
            if item.path.name == "trace.jsonl":
                score -= 20
            excerpt = line.strip()[:1200]
            matches.append(
                {
                    "log_id": item.log_id,
                    "alias": item.alias,
                    "line_number_tail_window": line_number,
                    "excerpt": excerpt,
                    "modified_at": item.modified_at,
                    "match_score": 1000 if exact else len(matched_terms),
                    "rank_score": score,
                    "matched_terms": matched_terms,
                }
            )
    matches.sort(
        key=lambda row: (
            -int(row.get("rank_score", row["match_score"])),
            -datetime.fromisoformat(str(row["modified_at"])).timestamp(),
            str(row["alias"]),
            int(row["line_number_tail_window"]),
        )
    )
    truncated = len(matches) > limit
    return {"matches": matches[:limit], "scanned_files": len(catalog), "truncated": truncated}


def open_log(
    log_id: str,
    *,
    files: dict[str, SafeLogFile] | None = None,
    start_line: int | None = None,
    max_lines: int = 80,
) -> dict[str, object]:
    catalog = files if files is not None else discover_logs()
    item = catalog.get(str(log_id))
    if item is None:
        raise ValueError("log_open_unknown_log")
    max_lines = max(1, min(int(max_lines), MAX_OPEN_LINES))
    try:
        text = redact_log_text(item.path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:
        raise ValueError("log_open_read_failed") from exc
    lines = text.splitlines()
    if start_line is None:
        start = max(0, len(lines) - max_lines)
    else:
        start = max(0, int(start_line) - 1)
    selected = lines[start : start + max_lines]
    rendered = "\n".join(selected)
    if len(rendered) > MAX_OPEN_CHARS:
        rendered = rendered[:MAX_OPEN_CHARS] + "\n<log_output_truncated>"
    return {
        "log_id": item.log_id,
        "alias": item.alias,
        "text": rendered,
        "start_line": start + 1,
        "end_line": start + len(selected),
        "total_lines": len(lines),
        "content_hash": hashlib.sha256(rendered.encode("utf-8", "replace")).hexdigest(),
        "modified_at": item.modified_at,
        "redacted": True,
    }


__all__ = [
    "MAX_LOG_FILES",
    "MAX_OPEN_CHARS",
    "MAX_OPEN_LINES",
    "SafeLogFile",
    "discover_logs",
    "open_log",
    "redact_log_text",
    "search_logs",
]
