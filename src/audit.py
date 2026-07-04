from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
import json
import os
import re


AUDIT_PATH = Path("logs/audit.jsonl")
SENSITIVE_KEY_RE = re.compile(r"(authorization|credential|password|secret|token)", re.I)
BODY_KEYS = {"body", "email_body", "full_body", "message_body"}
REDACTED = "[REDACTED]"


def log_operation(operation_type: str, data: dict[str, Any], audit_path: str | Path | None = None) -> Path:
    path = _resolve_audit_path(audit_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = redact_data(data)
    entry = {
        "timestamp": datetime.now().isoformat(),
        "operation": operation_type,
        "task_id": payload.get("task_id"),
        "confirmation_id": payload.get("confirmation_id"),
        "route": payload.get("route"),
        "evidence": payload.get("evidence"),
        "result_status": payload.get("result_status") or _extract_result_status(payload),
        "error": payload.get("error"),
        **payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, default=str, sort_keys=True) + "\n")
    return path


def redact_data(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                clean[key_text] = REDACTED
            else:
                clean[key_text] = redact_data(item)
        return clean
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return [redact_data(item) for item in value]
    return value


def _resolve_audit_path(audit_path: str | Path | None = None) -> Path:
    if audit_path is not None:
        return Path(audit_path)
    return Path(os.getenv("RALF_AUDIT_PATH") or AUDIT_PATH)


def _is_sensitive_key(key: str) -> bool:
    return bool(SENSITIVE_KEY_RE.search(key)) or key.lower() in BODY_KEYS


def _extract_result_status(payload: dict[str, Any]) -> Any:
    result = payload.get("result")
    if isinstance(result, dict):
        return result.get("status")
    confirmation = payload.get("confirmation")
    if isinstance(confirmation, dict):
        return confirmation.get("status")
    return None
