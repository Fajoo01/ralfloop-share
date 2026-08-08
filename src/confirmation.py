from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
import logging
import uuid

logger = logging.getLogger(__name__)


ConfirmationStatus = Literal["pending", "approved", "rejected"]


@dataclass
class PendingConfirmation:
    id: str
    action_type: str
    details: dict
    status: ConfirmationStatus = "pending"


@dataclass
class ConfirmationManager:
    pending: dict[str, PendingConfirmation] = field(default_factory=dict)

    def request_confirmation(self, action_type: str, details: dict) -> str:
        confirmation_id = str(uuid.uuid4())
        self.pending[confirmation_id] = PendingConfirmation(
            id=confirmation_id,
            action_type=action_type,
            details=details,
        )
        logger.info("confirmation_requested id=%s action=%s", confirmation_id, action_type)
        return confirmation_id

    def confirm_action(self, confirmation_id: str) -> bool:
        item = self.pending.get(confirmation_id)
        if item is None:
            return False
        item.status = "approved"
        logger.info("confirmation_approved id=%s", confirmation_id)
        return True

    def reject_action(self, confirmation_id: str) -> bool:
        item = self.pending.get(confirmation_id)
        if item is None:
            return False
        item.status = "rejected"
        logger.info("confirmation_rejected id=%s", confirmation_id)
        return True

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        return self.pending.get(confirmation_id)


confirmation_manager = ConfirmationManager()


def request_confirmation(action_type: str, details: dict) -> str:
    return confirmation_manager.request_confirmation(action_type, details)


def confirm_action(confirmation_id: str) -> bool:
    return confirmation_manager.confirm_action(confirmation_id)


def reject_action(confirmation_id: str) -> bool:
    return confirmation_manager.reject_action(confirmation_id)


def get_confirmation(confirmation_id: str) -> PendingConfirmation | None:
    return confirmation_manager.get(confirmation_id)
