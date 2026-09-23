from __future__ import annotations

import json
import os
import re
import tempfile
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = "bottazzi_gpt_handoff_v1"
SECRET_KEY_RE = re.compile(r"(?i)(authorization|cookie|credential|password|secret|token|storage_state)")


class GptSessionError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def default_state_dir() -> Path:
    state_home = os.getenv("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "bottazzi" / "gpt-session"


@dataclass(frozen=True)
class RolloverPolicy:
    max_turns: int = 36
    max_age_minutes: int = 120
    max_consecutive_errors: int = 2
    max_response_latency_ms: int = 30_000


@dataclass
class SessionMetrics:
    turns: int = 0
    age_minutes: int = 0
    consecutive_errors: int = 0
    last_response_latency_ms: int = 0
    phase_boundary: bool = False
    manual: bool = False


def session_metrics_from_ui(ui: Mapping[str, Any]) -> SessionMetrics:
    last_latency = int(ui.get("last_response_latency_ms") or 0)
    current_latency = int(ui.get("current_response_latency_ms") or 0)
    return SessionMetrics(
        turns=int(ui.get("user_turns") or 0),
        age_minutes=int(ui.get("page_age_minutes") or 0),
        consecutive_errors=int(ui.get("consecutive_errors") or 0),
        last_response_latency_ms=max(last_latency, current_latency),
    )


@dataclass(frozen=True)
class RolloverDecision:
    rollover: bool
    reasons: tuple[str, ...] = ()


def should_defer_latency_rollover(
    reasons: tuple[str, ...],
    *,
    response_pending: bool,
    response_in_progress: bool,
    response_idle_ms: int,
    max_idle_ms: int = 60_000,
    max_active_idle_ms: int = 600_000,
) -> bool:
    if reasons != ("latency_limit",):
        return False
    if not response_pending:
        return False
    idle_ms = max(0, response_idle_ms)
    if response_in_progress:
        return idle_ms < max_active_idle_ms
    return idle_ms < max_idle_ms


def evaluate_rollover(metrics: SessionMetrics, policy: RolloverPolicy | None = None) -> RolloverDecision:
    policy = policy or RolloverPolicy()
    reasons: list[str] = []
    if metrics.manual:
        reasons.append("manual")
    if metrics.turns >= policy.max_turns:
        reasons.append("turn_limit")
    if metrics.age_minutes >= policy.max_age_minutes:
        reasons.append("age_limit")
    if metrics.consecutive_errors >= policy.max_consecutive_errors:
        reasons.append("error_limit")
    if metrics.last_response_latency_ms >= policy.max_response_latency_ms:
        reasons.append("latency_limit")
    if metrics.phase_boundary and metrics.turns >= max(8, policy.max_turns // 2):
        reasons.append("phase_boundary")
    return RolloverDecision(bool(reasons), tuple(reasons))


@dataclass
class Handoff:
    goal: str
    current_state: str
    repo: dict[str, str] = field(default_factory=dict)
    constraints: list[str] = field(default_factory=list)
    completed: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    action_receipts: list[dict[str, str]] = field(default_factory=list)
    important_files: list[str] = field(default_factory=list)
    do_not_touch: list[str] = field(default_factory=list)
    open_problems: list[str] = field(default_factory=list)
    next_action: str = ""
    source_chat: str | None = None
    created_at: str = field(default_factory=_now)
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        _reject_secret_keys(data)
        if data["schema_version"] != SCHEMA_VERSION:
            raise GptSessionError("unsupported_handoff_schema")
        return data


class HandoffStore:
    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root).expanduser() if root else default_state_dir()

    @property
    def current_path(self) -> Path:
        return self.root / "current.json"

    @property
    def archive_dir(self) -> Path:
        return self.root / "archive"

    def save(self, handoff: Handoff) -> Path:
        payload = handoff.as_dict()
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise GptSessionError("handoff_too_large")
        self._ensure_dirs()
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        archive_path = self.archive_dir / f"handoff-{stamp}.json"
        _atomic_write(archive_path, encoded)
        _atomic_write(self.current_path, encoded)
        return archive_path

    def load_current(self) -> dict[str, Any]:
        path = self.current_path
        if path.is_symlink():
            raise GptSessionError("handoff_symlink_rejected")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise GptSessionError("handoff_not_found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise GptSessionError("handoff_invalid") from exc
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            raise GptSessionError("handoff_invalid")
        _reject_secret_keys(data)
        return data

    def update_source_chat(self, source_chat: str | None, source_chat_url: str | None = None) -> None:
        data = self.load_current()
        data["source_chat"] = source_chat
        data["source_chat_url"] = source_chat_url
        _reject_secret_keys(data)
        encoded = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise GptSessionError("handoff_too_large")
        self._ensure_dirs()
        _atomic_write(self.current_path, encoded)

    def render_prompt(self) -> str:
        data = self.load_current()
        repo = data.get("repo") or {}
        sections = [
            "Riprendi il lavoro da questo handoff Bot-tazzi. Non ripartire da zero.",
            f"GOAL\n{data.get('goal', '')}",
            f"STATO CORRENTE\n{data.get('current_state', '')}",
            "REPO / BRANCH / WORKTREE / COMMIT\n" + "\n".join(f"{k}: {v}" for k, v in repo.items()),
            "VINCOLI\n" + _bullets(data.get("constraints")),
            "COSE GIÀ FATTE\n" + _bullets(data.get("completed")),
            "RISULTATI TEST\n" + _bullets(data.get("tests")),
            "AZIONI ESTERNE GIÀ ESEGUITE\n" + _receipt_lines(data.get("action_receipts")),
            "FILE IMPORTANTI\n" + _bullets(data.get("important_files")),
            "COSE DA NON TOCCARE\n" + _bullets(data.get("do_not_touch")),
            "PROBLEMI APERTI\n" + _bullets(data.get("open_problems")),
            f"PROSSIMA AZIONE\n{data.get('next_action', '')}",
            "Prima di mutare lo stato, verifica Git/runtime reale e non ripetere azioni già presenti nelle ricevute.",
        ]
        return "\n\n".join(sections).strip() + "\n"

    def _ensure_dirs(self) -> None:
        if self.root.is_symlink() or self.archive_dir.is_symlink():
            raise GptSessionError("handoff_directory_invalid")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        os.chmod(self.archive_dir, 0o700)


def normalize_chatgpt_conversation_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
        return None
    match = re.fullmatch(r"(?:/g/[^/]+)?/c/([A-Za-z0-9-]+)", parsed.path.rstrip("/"))
    if not match:
        return None
    return f"https://chatgpt.com/c/{match.group(1)}"


def select_external_conversation(
    conversation_urls: list[str],
    *,
    seen_urls: list[str] | set[str],
    open_urls: list[str] | set[str],
    source_url: str | None,
) -> str | None:
    seen = {item for value in seen_urls if (item := normalize_chatgpt_conversation_url(value))}
    opened = {item for value in open_urls if (item := normalize_chatgpt_conversation_url(value))}
    source = normalize_chatgpt_conversation_url(source_url or "")
    ordered: list[str] = []
    for value in conversation_urls:
        candidate = normalize_chatgpt_conversation_url(value)
        if candidate and candidate not in ordered:
            ordered.append(candidate)
    if source:
        if source not in ordered:
            return None
        ordered = ordered[: ordered.index(source)]
    for candidate in ordered:
        if candidate not in seen and candidate not in opened and candidate != source:
            return candidate
    return None


class ExternalChatAdoptionStore:
    schema_version = "bottazzi_gpt_external_adoption_v1"

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root).expanduser() if root else default_state_dir()
        self.path = self.root / "external-chat-adoption.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": self.schema_version,
                "seen_conversations": [],
                "watcher_target_id": None,
                "last_adopted_conversation": None,
                "last_scan_epoch": 0,
            }
        if self.path.is_symlink():
            raise GptSessionError("external_adoption_symlink_rejected")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GptSessionError("external_adoption_invalid") from exc
        if not isinstance(data, dict) or data.get("schema_version") != self.schema_version:
            raise GptSessionError("external_adoption_invalid")
        _reject_secret_keys(data)
        return data

    def save(self, data: Mapping[str, Any]) -> None:
        payload = dict(data)
        payload["schema_version"] = self.schema_version
        seen: list[str] = []
        for value in payload.get("seen_conversations") or []:
            normalized = normalize_chatgpt_conversation_url(str(value))
            if normalized and normalized not in seen:
                seen.append(normalized)
        payload["seen_conversations"] = seen[-256:]
        _reject_secret_keys(payload)
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise GptSessionError("external_adoption_too_large")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        _atomic_write(self.path, encoded)


def _bullets(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "- nessuno"
    return "\n".join(f"- {str(value)}" for value in values)


def _receipt_lines(values: Any) -> str:
    if not isinstance(values, list) or not values:
        return "- nessuna"
    lines: list[str] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        label = value.get("action") or value.get("kind") or "azione"
        ref = value.get("ref") or value.get("url") or value.get("id") or ""
        status = value.get("status") or "done"
        lines.append(f"- {label}: {status}" + (f" [{ref}]" if ref else ""))
    return "\n".join(lines) if lines else "- nessuna"


def _reject_secret_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if SECRET_KEY_RE.search(str(key)):
                raise GptSessionError("secret_field_not_allowed")
            _reject_secret_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secret_keys(child)


def _atomic_write(path: Path, content: str) -> None:
    if path.exists() and path.is_symlink():
        raise GptSessionError("handoff_symlink_rejected")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
