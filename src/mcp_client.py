from __future__ import annotations

from typing import Any
import logging

from ralfloop_agent.integration.confirmation_store import execute_confirmed_action, request_confirmation
from src.confirmation import get_confirmation

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
            {"to": to, "subject": subject, "body_preview": body[:100]},
            action_fn=self._send_email_now,
            action_args={"to": to, "subject": subject, "body": body},
        )
        logger.info("mcp_send_email_blocked confirmation_id=%s", confirmation_id)
        raise NeedsConfirmationError(confirmation_id, "send_email")

    def send_telegram(self, message: str) -> str:
        confirmation_id = request_confirmation(
            "send_telegram",
            {"message_preview": message[:100]},
            action_fn=self._send_telegram_now,
            action_args={"message": message},
        )
        logger.info("mcp_send_telegram_blocked confirmation_id=%s", confirmation_id)
        raise NeedsConfirmationError(confirmation_id, "send_telegram")

    def drive_upload(self, content: str | bytes, filename: str = "upload.txt") -> str:
        size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
        confirmation_id = request_confirmation(
            "drive_upload",
            {"filename": filename, "size": size},
            action_fn=self._drive_upload_now,
            action_args={"content": content, "filename": filename},
        )
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
        return str(execute_confirmed_action(confirmation_id))

    def _send_email_now(self, to: str, subject: str, body: str) -> dict[str, Any]:
        try:
            from arclio_mcp_gsuite import GSuiteClient  # type: ignore
        except Exception:
            return {"status": "not_configured", "connector": "arclio_mcp_gsuite", "action": "send_email", "to": to}
        client = GSuiteClient()
        result = client.send_email(to=to, subject=subject, body=body)
        return {"status": "sent", "connector": "arclio_mcp_gsuite", "result": result}

    def _send_telegram_now(self, message: str) -> dict[str, Any]:
        return {"status": "not_configured", "connector": "telegram_mcp", "action": "send_telegram", "size": len(message)}

    def _drive_upload_now(self, content: str | bytes, filename: str) -> dict[str, Any]:
        try:
            from arclio_mcp_gsuite import GSuiteClient  # type: ignore
        except Exception:
            size = len(content.encode("utf-8")) if isinstance(content, str) else len(content)
            return {
                "status": "not_configured",
                "connector": "arclio_mcp_gsuite",
                "action": "drive_upload",
                "filename": filename,
                "size": size,
            }
        client = GSuiteClient()
        result = client.drive_upload(content=content, filename=filename)
        return {"status": "uploaded", "connector": "arclio_mcp_gsuite", "result": result}
