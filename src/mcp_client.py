from __future__ import annotations

import logging

from src.confirmation import get_confirmation, request_confirmation

logger = logging.getLogger(__name__)


class NeedsConfirmationError(RuntimeError):
    def __init__(self, confirmation_id: str, action_type: str) -> None:
        super().__init__(f"confirmation required for {action_type}: {confirmation_id}")
        self.confirmation_id = confirmation_id
        self.action_type = action_type


class MCPClient:
    def send_email(self, to: str, subject: str, body: str) -> str:
        confirmation_id = request_confirmation(
            "send_email",
            {"to": to, "subject": subject, "body": body},
        )
        logger.info("mcp_send_email_blocked confirmation_id=%s", confirmation_id)
        raise NeedsConfirmationError(confirmation_id, "send_email")

    def send_telegram(self, message: str) -> str:
        confirmation_id = request_confirmation("send_telegram", {"message": message})
        logger.info("mcp_send_telegram_blocked confirmation_id=%s", confirmation_id)
        raise NeedsConfirmationError(confirmation_id, "send_telegram")

    def drive_upload(self, content: str) -> str:
        confirmation_id = request_confirmation("drive_upload", {"content": content})
        logger.info("mcp_drive_upload_blocked confirmation_id=%s", confirmation_id)
        raise NeedsConfirmationError(confirmation_id, "drive_upload")

    def browser_inspect(self, url: str) -> str:
        logger.info("mcp_browser_inspect url=%s", url)
        return f"Browser inspect mock: {url}"

    def execute_confirmed(self, confirmation_id: str) -> str:
        item = get_confirmation(confirmation_id)
        if item is None:
            raise ValueError("unknown confirmation_id")
        if item.status != "approved":
            raise NeedsConfirmationError(confirmation_id, item.action_type)
        logger.info("mcp_execute_confirmed id=%s action=%s", confirmation_id, item.action_type)
        return f"Executed {item.action_type}: {item.details}"
