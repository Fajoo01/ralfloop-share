from __future__ import annotations

"""Strict WhatsApp MCP facade. No browser/CDP primitive crosses this boundary."""

from contextlib import AbstractContextManager
import os
import time
from typing import Any, Mapping

from ralfloop_agent.domains.domain_approval import effective_approval_status, scope_digest
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport


READ_TOOLS = frozenset({
    "whatsapp_list_chats",
    "whatsapp_search_chats",
    "whatsapp_open_chat",
    "whatsapp_read_messages",
    "whatsapp_search_messages",
    "whatsapp_read_history",
    "whatsapp_get_chat_info",
    "whatsapp_get_media",
    "whatsapp_get_document",
    "whatsapp_get_audio",
})
WRITE_TOOLS = frozenset({"whatsapp_send_message", "whatsapp_reply_message"})
ALL_TOOLS = READ_TOOLS | WRITE_TOOLS
FORBIDDEN_INPUTS = frozenset({
    "selector", "css", "xpath", "javascript", "js", "cdp", "cdp_method",
    "coordinate", "coordinates", "url",
})


class WhatsAppGatewayError(RuntimeError):
    pass


class WhatsAppGateway:
    def __init__(self, session: MCPClientSession) -> None:
        self.session = session
        self.discovered_tools: tuple[str, ...] = ()
        self._schemas: dict[str, Mapping[str, Any]] = {}

    def discover(self) -> tuple[str, ...]:
        tools = self.session.list_tools()
        names = {tool.name for tool in tools}
        missing = READ_TOOLS - names
        if missing:
            raise MCPProtocolError("whatsapp_read_tools_missing:" + ",".join(sorted(missing)))
        unexpected_generic = {
            name for name in names
            if any(term in name.casefold() for term in ("click", "evaluate", "navigate", "cdp", "javascript"))
        }
        if unexpected_generic:
            raise MCPProtocolError("whatsapp_generic_browser_tool_exposed")
        for tool in tools:
            schema = tool.input_schema
            properties = set((schema.get("properties") or {}).keys())
            if properties & FORBIDDEN_INPUTS:
                raise MCPProtocolError("whatsapp_unsafe_tool_schema")
            if schema.get("additionalProperties", True) is not False:
                raise MCPProtocolError("whatsapp_tool_schema_not_strict")
            self._schemas[tool.name] = schema
        self.discovered_tools = tuple(sorted(names & ALL_TOOLS))
        return self.discovered_tools

    def invoke_read(self, tool: str, **arguments: Any) -> Mapping[str, Any]:
        if tool not in READ_TOOLS:
            raise WhatsAppGatewayError("whatsapp_read_tool_denied")
        return self._call(tool, arguments)

    def execute_approved(
        self,
        store: DomainApprovalStore,
        request_id: str,
        current_scope: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        row = store.get_request(request_id)
        if not row:
            return {"status": "APPROVAL_INVALID", "sent": False, "retry_allowed": False}
        if effective_approval_status(row) != "approved":
            return {
                "status": "APPROVAL_INVALID", "sent": False, "retry_allowed": False,
                "approval_status": effective_approval_status(row),
            }
        if str(row.get("scope_digest") or "") != scope_digest(dict(current_scope)):
            store.mark_stale(request_id, ["whatsapp_artifact_changed"])
            return {"status": "DRAFT_CHANGED", "sent": False, "retry_allowed": False}
        scope = row.get("scope") or {}
        action = str(scope.get("action") or "")
        if action not in {"whatsapp_send", "whatsapp_reply"}:
            return {"status": "POLICY_DENIED", "sent": False, "retry_allowed": False}
        tool = "whatsapp_reply_message" if action == "whatsapp_reply" else "whatsapp_send_message"
        body = str(scope.get("body") or "")
        chat_id = str(scope.get("chat_id") or "")
        chat_title = str(scope.get("chat_title") or "")
        reply_to = str(scope.get("reply_to_message_id") or "")
        if not body.strip() or body != body.strip() or not chat_id or not chat_title:
            return {"status": "DRAFT_CHANGED", "sent": False, "retry_allowed": False}
        if action == "whatsapp_reply" and not reply_to:
            return {"status": "REPLY_TARGET_UNAVAILABLE", "sent": False, "retry_allowed": False}
        claim = store.claim_execution(request_id, action=action)
        if not claim.get("claimed"):
            return {
                "status": str(claim.get("status") or "EXECUTION_UNCERTAIN"),
                "sent": False, "retry_allowed": False,
            }
        arguments = {
            "execution_id": str(scope.get("execution_id") or request_id),
            "approval_request_id": request_id,
            "chat_id": chat_id,
            "chat_title": chat_title,
            "body": body,
            "draft_hash": str(scope.get("artifact_sha256") or ""),
        }
        if reply_to:
            arguments["message_id"] = reply_to
        try:
            raw = self._call(tool, arguments)
        except Exception:
            failure = {
                "status": "EXECUTION_UNCERTAIN", "sent": False,
                "verified": False, "retry_allowed": False,
            }
            store.finish_claimed_execution(
                request_id, action=action, success=False, result=failure,
            )
            return failure
        verified = bool(raw.get("verified"))
        message_id = str(raw.get("message_id") or "")
        exact_observed = bool(raw.get("exact_text_observed"))
        if not verified or not (message_id or exact_observed):
            failure = {
                "status": "SEND_UNVERIFIED", "sent": False, "verified": False,
                "retry_allowed": False,
            }
            store.finish_claimed_execution(
                request_id, action=action, success=False, result=failure,
            )
            return failure
        success = {
            "status": "executed", "sent": True, "verified": True,
            "execution_id": arguments["execution_id"], "chat_id": chat_id,
            "message_id": message_id, "approved_hash": arguments["draft_hash"],
            "approved_version": int(scope.get("draft_version") or 1),
            "provider_confirmed_at": int(time.time()), "retry_allowed": False,
        }
        completed = store.finish_claimed_execution(
            request_id, action=action, success=True, result=success,
        )
        if completed.get("status") != "consumed":
            return {"status": "EXECUTION_UNCERTAIN", "sent": False, "retry_allowed": False}
        return success

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        if tool not in self.discovered_tools:
            raise MCPProtocolError("whatsapp_tool_not_discovered")
        if set(arguments) & FORBIDDEN_INPUTS:
            raise WhatsAppGatewayError("whatsapp_unsafe_argument")
        schema = self._schemas.get(tool) or {}
        allowed = set((schema.get("properties") or {}).keys())
        if set(arguments) - allowed:
            raise WhatsAppGatewayError("whatsapp_argument_not_allowlisted")
        result = self.session.call_tool(tool, dict(arguments))
        structured = result.get("structuredContent") if isinstance(result, Mapping) else None
        payload = structured if isinstance(structured, Mapping) else result
        if not isinstance(payload, Mapping):
            raise MCPProtocolError("whatsapp_result_not_object")
        if payload.get("side_effects", 0) not in {0, None} and tool in READ_TOOLS:
            raise MCPProtocolError("whatsapp_read_reported_side_effect")
        return dict(payload)


class WhatsAppMCPContext(AbstractContextManager[WhatsAppGateway]):
    def __init__(self, socket_path: str, timeout: float = 20.0) -> None:
        self.socket_path = socket_path
        self.timeout = timeout
        self.session: MCPClientSession | None = None

    @classmethod
    def from_environment(cls) -> "WhatsAppMCPContext":
        return cls(
            os.getenv("RALF_WHATSAPP_MCP_SOCKET", "/run/ralf-whatsapp-mcp/mcp.sock"),
            float(os.getenv("RALF_WHATSAPP_MCP_TIMEOUT", "20")),
        )

    def __enter__(self) -> WhatsAppGateway:
        self.session = MCPClientSession(UnixMCPTransport(self.socket_path), timeout=self.timeout)
        self.session.__enter__()
        gateway = WhatsAppGateway(self.session)
        gateway.discover()
        return gateway

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.session is not None:
            self.session.__exit__(exc_type, exc, tb)


__all__ = [
    "ALL_TOOLS", "READ_TOOLS", "WRITE_TOOLS", "WhatsAppGateway",
    "WhatsAppGatewayError", "WhatsAppMCPContext",
]
