from __future__ import annotations

import json
import os
import re
import tempfile
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SECRET_KEYS = ("authorization", "cookie", "credential", "hmac", "password", "secret", "token")
SECRET_VALUE_RE = re.compile(
    r"(?i)(\b(?:api[\s_-]?key|authorization|cookie|credential|hmac|pass(?:word)?|"
    r"secret(?:\s+\w+)?|token(?:\s+\w+)?)\b\s*(?::|=|\bis\b)\s*)([^\r\n]+)"
)
BEARER_RE = re.compile(r"(?i)(\bbearer\s+)([^\s,;]+)")
PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"-----END(?: [A-Z0-9]+)? PRIVATE KEY-----",
    re.DOTALL,
)
KNOWN_SECRET_RE = re.compile(
    r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9_-]{8,}|"
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{12,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{12,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}|"
    r"\bAKIA[A-Z0-9]{16}\b"
)
JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
HIGH_ENTROPY_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9_+=./-]{24,})(?![A-Za-z0-9])")
STORED_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
ALLOWED_RECORD_KEYS = {
    "session_id",
    "created_at",
    "updated_at",
    "cwd",
    "history",
    "model",
    "context_enabled",
    "metadata",
    "assistant_state",
}

ASSISTANT_STATE_KEYS = {"schema_version", "last_intent", "last_entities", "pending"}
PENDING_STATE_KEYS = {
    "email", "whatsapp", "mailchimp", "jellyfin", "browser", "runts", "home", "infrastructure", "bandi", "clarification",
}
PENDING_ACTION_KEYS = {
    "pending_id",
    "domain",
    "action",
    "policy",
    "payload",
    "payload_digest",
    "displayed_digest",
    "version",
    "created_at",
    "expires_at",
    "approval_ref",
    "approved_digest",
}


class SessionStoreError(ValueError):
    pass


def default_sessions_dir() -> Path:
    state_home = os.getenv("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "ralf" / "sessions"


def default_introspection_dir() -> Path:
    configured = os.getenv("RALF_SESSION_INTROSPECTION_DIR")
    if configured:
        return Path(configured).expanduser()
    data_home = os.getenv("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "ralf" / "session-introspection"


class SessionStore:
    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        introspection_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = Path(root).expanduser() if root is not None else default_sessions_dir()
        if introspection_root is not None:
            self.introspection_root: Path | None = Path(introspection_root).expanduser()
        elif root is None:
            self.introspection_root = default_introspection_dir()
        else:
            self.introspection_root = None

    def create(self, *, cwd: str, model: str | None = None, context_enabled: bool = True) -> dict[str, Any]:
        now = _timestamp()
        record: dict[str, Any] = {
            "session_id": uuid4().hex,
            "created_at": now,
            "updated_at": now,
            "cwd": cwd,
            "history": [],
            "model": model,
            "context_enabled": bool(context_enabled),
            "metadata": {},
        }
        self.save(record)
        return record

    def save(self, record: dict[str, Any]) -> None:
        session_id = _validate_session_id(record.get("session_id"))
        _reject_secret_keys(record)
        payload = _normalize_record(deepcopy(record), session_id=session_id)
        payload["updated_at"] = _timestamp()
        payload.setdefault("created_at", payload["updated_at"])
        payload.setdefault("history", [])
        payload.setdefault("metadata", {})
        self._ensure_root()

        fd, temp_name = tempfile.mkstemp(prefix=f".{session_id}.", suffix=".tmp", dir=self.root)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._path(session_id))
            os.chmod(self._path(session_id), 0o600)
            _fsync_directory(self.root)
            record.clear()
            record.update(payload)
            self._sync_introspection(payload)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def load(self, session_id: str) -> dict[str, Any]:
        path = self._path(_validate_session_id(session_id))
        if path.is_symlink():
            raise SessionStoreError("session_invalid")
        try:
            with path.open("r", encoding="utf-8") as handle:
                record = json.load(handle)
        except FileNotFoundError as exc:
            raise SessionStoreError("session_not_found") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionStoreError("session_invalid") from exc
        if not isinstance(record, dict) or record.get("session_id") != session_id:
            raise SessionStoreError("session_invalid")
        _reject_secret_keys(record)
        return _normalize_record(record, session_id=session_id)

    def latest(self) -> dict[str, Any]:
        records = self.list()
        if not records:
            raise SessionStoreError("session_not_found")
        return self.load(records[0]["session_id"])

    def list(self) -> list[dict[str, Any]]:
        if not self.root.exists():
            return []
        rows: list[dict[str, Any]] = []
        for path in self.root.glob("*.json"):
            if path.is_symlink():
                continue
            try:
                with path.open("r", encoding="utf-8") as handle:
                    record = json.load(handle)
                session_id = _validate_session_id(record.get("session_id"))
            except (OSError, ValueError, json.JSONDecodeError, SessionStoreError):
                continue
            rows.append(
                {
                    "session_id": session_id,
                    "updated_at": str(record.get("updated_at") or ""),
                    "cwd": str(record.get("cwd") or ""),
                    "model": record.get("model"),
                    "turns": len(record.get("history") or []) // 2,
                }
            )
        return sorted(rows, key=lambda row: row["updated_at"], reverse=True)

    def delete(self, session_id: str) -> None:
        session_id = _validate_session_id(session_id)
        try:
            self._path(session_id).unlink()
        except FileNotFoundError as exc:
            raise SessionStoreError("session_not_found") from exc
        if self.introspection_root is not None:
            try:
                self._introspection_path(session_id).unlink()
            except FileNotFoundError:
                pass

    def _sync_introspection(self, payload: dict[str, Any]) -> None:
        if self.introspection_root is None:
            return
        session_id = _validate_session_id(payload.get("session_id"))
        root = self.introspection_root
        if root.is_symlink():
            raise SessionStoreError("introspection_directory_invalid")
        root.mkdir(parents=True, exist_ok=True, mode=0o750)
        os.chmod(root, 0o750)
        path = self._introspection_path(session_id)
        previous_history: list[dict[str, str]] = []
        if path.exists():
            if path.is_symlink():
                raise SessionStoreError("introspection_invalid")
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(existing, dict) and existing.get("session_id") == session_id:
                    previous_history = _sanitize_history(existing.get("history"))
            except (OSError, json.JSONDecodeError):
                previous_history = []
        current_history = _sanitize_history(payload.get("history"))
        merged_history = _merge_history(previous_history, current_history)
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        archive = {
            "schema_version": "ralf_session_transcript_v1",
            "session_id": session_id,
            "created_at": str(payload.get("created_at") or ""),
            "updated_at": str(payload.get("updated_at") or ""),
            "cwd": _safe_scalar(payload.get("cwd") or ""),
            "model": _safe_scalar(payload["model"]) if payload.get("model") else None,
            "provider": _safe_scalar(metadata["provider"]) if metadata.get("provider") else None,
            "history": merged_history,
        }
        fd, temp_name = tempfile.mkstemp(prefix=f".{session_id}.", suffix=".tmp", dir=root)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o640)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(archive, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            os.chmod(path, 0o640)
            _fsync_directory(root)
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _introspection_path(self, session_id: str) -> Path:
        if self.introspection_root is None:
            raise SessionStoreError("introspection_disabled")
        return self.introspection_root / f"{session_id}.json"

    def _ensure_root(self) -> None:
        if self.root.is_symlink():
            raise SessionStoreError("session_directory_invalid")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id}.json"


def _validate_session_id(value: Any) -> str:
    session_id = str(value or "")
    if not SESSION_ID_RE.fullmatch(session_id):
        raise SessionStoreError("invalid_session_id")
    return session_id


def _reject_secret_keys(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(marker in lowered for marker in SECRET_KEYS):
                raise SessionStoreError("secret_metadata_not_allowed")
            _reject_secret_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_secret_keys(item)


def _normalize_record(record: dict[str, Any], *, session_id: str) -> dict[str, Any]:
    if any(key not in ALLOWED_RECORD_KEYS for key in record):
        raise SessionStoreError("unsupported_session_field")
    metadata = record.get("metadata")
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict) or any(key != "provider" for key in metadata):
        raise SessionStoreError("unsupported_session_metadata")
    provider = metadata.get("provider")
    safe_metadata = {"provider": _safe_scalar(provider)} if provider else {}
    result = {
        "session_id": session_id,
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "cwd": _safe_scalar(record.get("cwd") or ""),
        "history": _sanitize_history(record.get("history")),
        "model": _safe_scalar(record["model"]) if record.get("model") else None,
        "context_enabled": bool(record.get("context_enabled", True)),
        "metadata": safe_metadata,
    }
    if record.get("assistant_state") is not None:
        result["assistant_state"] = _sanitize_assistant_state(record["assistant_state"])
    return result


def _sanitize_assistant_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - ASSISTANT_STATE_KEYS:
        raise SessionStoreError("unsupported_assistant_state")
    if value.get("schema_version") != "unified_conversation_v1":
        raise SessionStoreError("unsupported_assistant_state")
    last_entities = value.get("last_entities") or {}
    pending = value.get("pending") or {}
    if not isinstance(last_entities, dict) or len(last_entities) > 8:
        raise SessionStoreError("unsupported_assistant_state")
    if not isinstance(pending, dict) or set(pending) - PENDING_STATE_KEYS:
        raise SessionStoreError("unsupported_assistant_state")
    safe_entities: dict[str, list[str]] = {}
    for domain, entities in last_entities.items():
        if domain not in PENDING_STATE_KEYS | {"personal_relational", "tiremm", "research", "code", "media", "general_assistant"}:
            raise SessionStoreError("unsupported_assistant_state")
        if not isinstance(entities, (list, tuple)) or len(entities) > 8:
            raise SessionStoreError("unsupported_assistant_state")
        safe_entities[domain] = [_bounded_text(item, 240) for item in entities]
    safe_pending: dict[str, Any] = {name: None for name in PENDING_STATE_KEYS}
    for domain, item in pending.items():
        if item is None:
            continue
        if not isinstance(item, dict) or set(item) != PENDING_ACTION_KEYS or item.get("domain") != domain:
            raise SessionStoreError("unsupported_assistant_state")
        safe_pending[domain] = {
            key: _sanitize_state_value(child, depth=0)
            for key, child in item.items()
        }
    result = {
        "schema_version": "unified_conversation_v1",
        "last_intent": _bounded_text(value.get("last_intent"), 96) if value.get("last_intent") else None,
        "last_entities": safe_entities,
        "pending": safe_pending,
    }
    if len(json.dumps(result, ensure_ascii=False)) > 32_768:
        raise SessionStoreError("assistant_state_too_large")
    return result


def _sanitize_state_value(value: Any, *, depth: int) -> Any:
    if depth > 6:
        raise SessionStoreError("assistant_state_too_deep")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value, 12_000)
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            raise SessionStoreError("assistant_state_too_large")
        return [_sanitize_state_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 64:
            raise SessionStoreError("assistant_state_too_large")
        _reject_secret_keys(value)
        return {
            _bounded_text(key, 96): _sanitize_state_value(item, depth=depth + 1)
            for key, item in value.items()
        }
    raise SessionStoreError("unsupported_assistant_state_value")


def _bounded_text(value: Any, maximum: int) -> str:
    text = _safe_scalar(value)
    if len(text) > maximum:
        raise SessionStoreError("assistant_state_value_too_large")
    return text


def _safe_scalar(value: Any) -> str:
    text = str(value)
    if STORED_CONTROL_RE.search(text) or _redact_known(text) != text:
        raise SessionStoreError("secret_value_not_allowed")
    return text


def _sanitize_history(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    history: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        if role not in {"user", "assistant"}:
            continue
        content = str(item.get("content") or "")
        content = _redact_text(content)
        history.append({"role": role, "content": content})
    return history



def _merge_history(previous: list[dict[str, str]], current: list[dict[str, str]]) -> list[dict[str, str]]:
    if not current:
        return list(previous)
    if not previous:
        return list(current)
    maximum = min(len(previous), len(current))
    overlap = 0
    for size in range(maximum, 0, -1):
        if previous[-size:] == current[:size]:
            overlap = size
            break
    return [*previous, *current[overlap:]]

def _redact_text(text: str) -> str:
    text = STORED_CONTROL_RE.sub("", text)
    text = _redact_known(text)

    def redact_high_entropy(match: re.Match[str]) -> str:
        value = match.group(1)
        return "[REDACTED]" if any(char.isalpha() for char in value) and any(char.isdigit() for char in value) else value

    return HIGH_ENTROPY_RE.sub(redact_high_entropy, text)


def _redact_known(text: str) -> str:
    text = PRIVATE_KEY_RE.sub("[REDACTED PRIVATE KEY]", text)
    text = SECRET_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = BEARER_RE.sub(r"\1[REDACTED]", text)
    text = KNOWN_SECRET_RE.sub("[REDACTED]", text)
    return JWT_RE.sub("[REDACTED]", text)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()
