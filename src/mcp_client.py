from __future__ import annotations

from typing import Any
import json
import logging
import os
from urllib.parse import urlparse

from ralfloop_agent.integration.confirmation_store import execute_confirmed_action, request_confirmation
from src import audit
from src.confirmation import get_confirmation

logger = logging.getLogger(__name__)


class NeedsConfirmationError(RuntimeError):
    def __init__(self, confirmation_id: str, action_type: str) -> None:
        super().__init__(f"confirmation required for {action_type}: {confirmation_id}")
        self.confirmation_id = confirmation_id
        self.action_type = action_type


class MCPClient:
    def __init__(
        self,
        google_client: Any | None = None,
        *,
        arci_gateway: Any | None = None,
    ) -> None:
        enabled_flag = os.getenv("RALF_MCP_GOOGLE_ENABLED")
        self._google_explicitly_disabled = enabled_flag is not None and enabled_flag != "1"
        self.google_enabled = enabled_flag == "1" or google_client is not None
        if google_client is not None:
            self.google = google_client
        else:
            self.google = None
        self.arci_gateway = arci_gateway

    def send_email(self, to: str, subject: str, body: str) -> str:
        if self._google_explicitly_disabled:
            raise NotImplementedError("Google Email MCP is disabled")
        if self.google_enabled and (self.google is None or not self.google.is_configured()):
            raise NotImplementedError("Google Email MCP is not configured")
        confirmation_id = request_confirmation(
            "send_email",
            {
                "to": to,
                "subject": subject,
                "body_preview": body[:100],
                "draft_only": getattr(self.google, "draft_only", True),
            },
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
        if _is_arci_members_url(url):
            return json.dumps(
                self.arci_organization_profile(),
                ensure_ascii=False,
                sort_keys=True,
            )
        return f"Browser inspect mock: {url}"

    def arci_organization_profile(self) -> dict[str, Any]:
        if self.arci_gateway is not None:
            return dict(self.arci_gateway.read_organization_profile())

        from src.arci import ArciMCPContext

        with ArciMCPContext.from_environment() as gateway:
            return dict(gateway.read_organization_profile())

    def execute_confirmed(self, confirmation_id: str) -> str:
        item = get_confirmation(confirmation_id)
        if item is None:
            raise ValueError("unknown confirmation_id")
        if item.status != "approved":
            raise NeedsConfirmationError(confirmation_id, item.action_type)
        logger.info("mcp_execute_confirmed id=%s action=%s", confirmation_id, item.action_type)
        return str(execute_confirmed_action(confirmation_id))

    def _send_email_now(self, to: str, subject: str, body: str) -> dict[str, Any]:
        draft_only = getattr(self.google, "draft_only", True)
        try:
            if self.google is None or not self.google.is_configured():
                raise RuntimeError("legacy_direct_google_fallback_disabled")
            if draft_only:
                result = self.google.create_draft(to, subject, body)
            else:
                result = self.google.send_email(to, subject, body)
            audit.log_operation(
                "mcp_send_email_executed",
                {
                    "action": "send_email",
                    "to": to,
                    "subject": subject,
                    "draft_only": draft_only,
                    "result_status": result.get("status"),
                    "message_id": result.get("id"),
                },
            )
            return result
        except Exception as exc:
            audit.log_operation(
                "mcp_send_email_failed",
                {"action": "send_email", "to": to, "subject": subject, "draft_only": draft_only, "error": str(exc)},
            )
            raise

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


def _is_arci_members_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").casefold() == "portale.arci.it"
        and port in {None, 443}
        and parsed.username is None
        and parsed.password is None
        and parsed.path == "/admin/office/circolosoci/"
        and not parsed.params
        and not parsed.query
        and not parsed.fragment
    )
