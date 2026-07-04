from __future__ import annotations

from collections.abc import Callable
from typing import Any

from src.confirmation import (
    confirmation_manager,
    get_confirmation,
    request_confirmation as _request_confirmation,
)


pending = confirmation_manager.pending
pending_actions: dict[str, dict[str, Any]] = {}


def request_confirmation(
    action_type: str,
    details: dict,
    action_fn: Callable[..., Any] | None = None,
    action_args: dict | None = None,
) -> str:
    confirmation_id = _request_confirmation(action_type, details)
    pending_actions[confirmation_id] = {
        "action_type": action_type,
        "details": details,
        "action_fn": action_fn,
        "action_args": action_args or {},
        "status": "pending",
        "executed": False,
        "result": None,
    }
    return confirmation_id


def confirm_action(confirmation_id: str) -> bool:
    action = pending_actions.get(confirmation_id)
    if action is None:
        return confirmation_manager.confirm_action(confirmation_id)
    if action["status"] == "rejected":
        return False
    ok = confirmation_manager.confirm_action(confirmation_id)
    if not ok:
        return False
    action["status"] = "approved"
    execute_confirmed_action(confirmation_id)
    return True


def reject_action(confirmation_id: str) -> bool:
    action = pending_actions.get(confirmation_id)
    if action is not None:
        action["status"] = "rejected"
    return confirmation_manager.reject_action(confirmation_id)


def execute_confirmed_action(confirmation_id: str) -> Any:
    action = pending_actions.get(confirmation_id)
    if action is None:
        confirmation = get_confirmation(confirmation_id)
        if confirmation is None:
            raise ValueError("unknown confirmation_id")
        return {"status": confirmation.status, "action_type": confirmation.action_type}
    if action["executed"]:
        return action["result"]
    confirmation = get_confirmation(confirmation_id)
    if confirmation is None or confirmation.status != "approved":
        raise ValueError("confirmation is not approved")
    if action["action_fn"] is None:
        result = {"status": "approved", "action_type": action["action_type"], "executed": False}
    else:
        result = action["action_fn"](**action["action_args"])
    action["executed"] = True
    action["result"] = result
    return result


__all__ = [
    "confirm_action",
    "execute_confirmed_action",
    "get_confirmation",
    "pending",
    "pending_actions",
    "reject_action",
    "request_confirmation",
]
