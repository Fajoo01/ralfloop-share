from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
import json
import os
import sqlite3
import tempfile

from src.confirmation import (
    confirmation_manager,
    get_confirmation,
    request_confirmation as _request_confirmation,
)


DEFAULT_DB_PATH = Path("data/ralf_confirmations.sqlite")
FINAL_STATUSES = {"executed", "failed", "rejected"}

pending = confirmation_manager.pending
pending_actions: dict[str, dict[str, Any]] = {}


def request_confirmation(
    action_type: str,
    details: dict,
    action_fn: Callable[..., Any] | None = None,
    action_args: dict | None = None,
) -> str:
    confirmation_id = _request_confirmation(action_type, details)
    now = _now()
    pending_actions[confirmation_id] = {
        "action_type": action_type,
        "details": details,
        "action_fn": action_fn,
        "action_args": action_args or {},
        "status": "pending",
        "executed": False,
        "result": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
    }
    _upsert_persistent(
        confirmation_id,
        action_type=action_type,
        details=details,
        status="pending",
        executed=False,
        result=None,
        error=None,
        created_at=now,
        updated_at=now,
    )
    return confirmation_id


def confirm_action(confirmation_id: str) -> bool:
    action = pending_actions.get(confirmation_id)
    if action is None:
        persisted = get_persistent_confirmation(confirmation_id)
        if persisted and persisted["status"] in FINAL_STATUSES:
            return False
        ok = confirmation_manager.confirm_action(confirmation_id)
        if ok:
            _update_persistent(confirmation_id, status="approved", updated_at=_now())
        return ok

    if action["status"] in {"failed", "rejected"}:
        return False
    if action["executed"] or action["status"] == "executed":
        return True

    ok = confirmation_manager.confirm_action(confirmation_id)
    if not ok:
        return False
    action["status"] = "approved"
    action["updated_at"] = _now()
    _update_persistent(confirmation_id, status="approved", updated_at=action["updated_at"])
    try:
        execute_confirmed_action(confirmation_id)
    except Exception:
        return False
    return True


def reject_action(confirmation_id: str) -> bool:
    action = pending_actions.get(confirmation_id)
    if action is not None:
        if action["executed"] or action["status"] in {"executed", "failed"}:
            return False
        action["status"] = "rejected"
        action["updated_at"] = _now()
    ok = confirmation_manager.reject_action(confirmation_id)
    if ok or action is not None:
        _update_persistent(confirmation_id, status="rejected", updated_at=_now())
        return True
    persisted = get_persistent_confirmation(confirmation_id)
    if persisted and persisted["status"] == "pending":
        _update_persistent(confirmation_id, status="rejected", updated_at=_now())
        return True
    return False


def execute_confirmed_action(confirmation_id: str) -> Any:
    action = pending_actions.get(confirmation_id)
    if action is None:
        confirmation = get_confirmation(confirmation_id)
        if confirmation is None:
            raise ValueError("unknown confirmation_id")
        return {"status": confirmation.status, "action_type": confirmation.action_type}

    if action["executed"]:
        return action["result"]
    if action["status"] == "failed":
        raise ValueError("confirmation action failed")
    if action["status"] == "rejected":
        raise ValueError("confirmation action rejected")

    confirmation = get_confirmation(confirmation_id)
    if confirmation is None or confirmation.status != "approved":
        raise ValueError("confirmation is not approved")

    try:
        if action["action_fn"] is None:
            result = {"status": "approved", "action_type": action["action_type"], "executed": False}
        else:
            result = action["action_fn"](**action["action_args"])
    except Exception as exc:
        action["status"] = "failed"
        action["error"] = str(exc)
        action["updated_at"] = _now()
        _update_persistent(
            confirmation_id,
            status="failed",
            error=action["error"],
            updated_at=action["updated_at"],
        )
        _audit_confirmation("confirmation_failed", confirmation_id, action)
        raise

    action["executed"] = True
    action["status"] = "executed"
    action["result"] = result
    action["updated_at"] = _now()
    _update_persistent(
        confirmation_id,
        status="executed",
        executed=True,
        result=result,
        error=None,
        updated_at=action["updated_at"],
    )
    _audit_confirmation("confirmation_executed", confirmation_id, action)
    return result


def get_persistent_confirmation(confirmation_id: str) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute(
            "select * from confirmations where confirmation_id = ?",
            (confirmation_id,),
        ).fetchone()
    return _row_to_dict(row) if row is not None else None


def _upsert_persistent(
    confirmation_id: str,
    *,
    action_type: str,
    details: dict,
    status: str,
    executed: bool,
    result: Any,
    error: str | None,
    created_at: str,
    updated_at: str,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            insert into confirmations (
                confirmation_id, action_type, details_json, status, executed,
                result_json, error, created_at, updated_at
            )
            values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            on conflict(confirmation_id) do update set
                action_type=excluded.action_type,
                details_json=excluded.details_json,
                status=excluded.status,
                executed=excluded.executed,
                result_json=excluded.result_json,
                error=excluded.error,
                updated_at=excluded.updated_at
            """,
            (
                confirmation_id,
                action_type,
                _json_dumps(details),
                status,
                int(executed),
                _json_dumps(result),
                error,
                created_at,
                updated_at,
            ),
        )


def _update_persistent(confirmation_id: str, **fields: Any) -> None:
    if not fields:
        return
    allowed = {"status", "executed", "result", "error", "updated_at"}
    updates = {key: value for key, value in fields.items() if key in allowed}
    if not updates:
        return
    column_values: dict[str, Any] = {}
    for key, value in updates.items():
        if key == "result":
            column_values["result_json"] = _json_dumps(value)
        elif key == "executed":
            column_values["executed"] = int(bool(value))
        else:
            column_values[key] = value
    assignments = ", ".join(f"{key}=?" for key in column_values)
    values = list(column_values.values()) + [confirmation_id]
    with _connect() as conn:
        conn.execute(
            f"update confirmations set {assignments} where confirmation_id = ?",
            values,
        )


def _connect() -> sqlite3.Connection:
    path = _resolved_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        create table if not exists confirmations (
            confirmation_id text primary key,
            action_type text not null,
            details_json text not null,
            status text not null,
            executed integer not null default 0,
            result_json text,
            error text,
            created_at text not null,
            updated_at text not null
        )
        """
    )


def _db_path() -> Path:
    return Path(os.getenv("RALF_CONFIRMATION_DB_PATH") or DEFAULT_DB_PATH)


def _resolved_db_path() -> Path:
    path = _db_path()
    if os.getenv("RALF_CONFIRMATION_DB_PATH"):
        return path
    if _sqlite_path_writable(path):
        return path
    return Path(tempfile.gettempdir()) / f"ralf_confirmations_{os.getuid()}.sqlite"


def _sqlite_path_writable(path: Path) -> bool:
    parent = path.parent if path.parent != Path("") else Path(".")
    if path.exists():
        return os.access(path, os.R_OK | os.W_OK) and os.access(parent, os.W_OK)
    if parent.exists():
        return os.access(parent, os.W_OK)
    return os.access(parent.parent, os.W_OK)


def _json_dumps(value: Any) -> str:
    from src.audit import redact_data

    return json.dumps(redact_data(value), ensure_ascii=False, default=str, sort_keys=True)


def _json_loads(value: str | None) -> Any:
    if not value:
        return None
    return json.loads(value)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "confirmation_id": row["confirmation_id"],
        "action_type": row["action_type"],
        "details": _json_loads(row["details_json"]),
        "status": row["status"],
        "executed": bool(row["executed"]),
        "result": _json_loads(row["result_json"]),
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _now() -> str:
    return datetime.now().isoformat()


def _audit_confirmation(operation: str, confirmation_id: str, action: dict[str, Any]) -> None:
    try:
        from src import audit

        audit.log_operation(
            operation,
            {
                "confirmation_id": confirmation_id,
                "action_type": action.get("action_type"),
                "status": action.get("status"),
                "executed": action.get("executed"),
                "result": action.get("result"),
                "result_status": (action.get("result") or {}).get("status")
                if isinstance(action.get("result"), dict)
                else None,
                "error": action.get("error"),
            },
        )
    except Exception:
        return


__all__ = [
    "confirm_action",
    "execute_confirmed_action",
    "get_confirmation",
    "get_persistent_confirmation",
    "pending",
    "pending_actions",
    "reject_action",
    "request_confirmation",
]
