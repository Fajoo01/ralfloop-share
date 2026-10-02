from __future__ import annotations

import os
from typing import Any, Mapping

from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport


class GoogleWorkspaceGmailDraftSync:
    TOOL = "upsert_email_draft"

    def __init__(
        self,
        socket_path: str | None = None,
        *,
        account: str | None = None,
        timeout: float | None = None,
    ) -> None:
        self.socket_path = socket_path or os.getenv(
            "RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock"
        )
        self.account = account or os.getenv(
            "RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com"
        )
        self.timeout = float(timeout or os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "20"))

    @classmethod
    def from_environment(cls) -> "GoogleWorkspaceGmailDraftSync":
        return cls()

    def upsert(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        draft_id: str = "",        cc: str = "",
        bcc: str = "",
        thread_id: str = "",
        attachments: tuple[Mapping[str, Any], ...] = (),
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "email": self.account,
            "to": to,
            "subject": subject,
            "body": body,
        }
        if draft_id:
            arguments["draftId"] = draft_id
        if cc:
            arguments["cc"] = cc
        if bcc:
            arguments["bcc"] = bcc
        if thread_id:
            arguments["threadId"] = thread_id
        if attachments:
            arguments["attachments"] = [dict(item) for item in attachments]
        with MCPClientSession(
            UnixMCPTransport(self.socket_path, connect_timeout=0.8),
            timeout=self.timeout,
            client_name="ralfloop-gmail-draft-sync",
        ) as client:
            names = {item.name for item in client.list_tools()}
            if self.TOOL not in names:
                raise MCPProtocolError("gmail_draft_tool_not_discovered")
            result = client.call_tool(self.TOOL, arguments)
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            raise MCPProtocolError("gmail_draft_tool_malformed")
        payload = dict(structured)
        if result.get("isError") or payload.get("sent") is not False:
            raise MCPProtocolError("gmail_draft_tool_unsafe_result")
        draft_id_out = str(payload.get("draftId") or "")
        if not draft_id_out:
            raise MCPProtocolError("gmail_draft_id_missing")
        return payload

    def send_bound(
        self, *, draft_id: str, approved_raw_sha256: str, execution_id: str
    ) -> dict[str, Any]:
        arguments = {
            "email": self.account, "draftId": draft_id,
            "approvedRawSha256": approved_raw_sha256,
            "executionId": execution_id,
        }
        with MCPClientSession(
            UnixMCPTransport(self.socket_path, connect_timeout=0.8),
            timeout=self.timeout, client_name="ralfloop-gmail-draft-send",
        ) as client:
            names = {item.name for item in client.list_tools()}
            if "send_email_draft" not in names:
                raise MCPProtocolError("gmail_draft_send_tool_not_discovered")
            result = client.call_tool("send_email_draft", arguments)
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            raise MCPProtocolError("gmail_draft_send_tool_malformed")
        payload = dict(structured)
        if result.get("isError") or payload.get("sent") is not True:
            raise MCPProtocolError("gmail_draft_send_unverified")
        return payload