#!/usr/bin/env python3
from __future__ import annotations

"""Semantic WhatsApp MCP server. Browser selectors/CDP never enter tool schemas."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.whatsapp_web import WhatsAppWebReadOnly
from ralfloop_agent.unified_assistant.whatsapp_writer import WhatsAppBrowserWriter
from src.mcp_transport import MCP_PROTOCOL_VERSION


def _schema(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "type": "object", "properties": dict(properties),
        "required": list(required), "additionalProperties": False,
    }


TEXT = {"type": "string", "minLength": 1, "maxLength": 4000}
CHAT = {"type": "string", "pattern": r"^wa_chat_[a-f0-9]{16}$"}
MESSAGE = {"type": "string", "pattern": r"^wa_msg_[a-f0-9]{16}$"}
LIMIT = {"type": "integer", "minimum": 1, "maximum": 500}


TOOLS: dict[str, dict[str, Any]] = {
    "whatsapp_list_chats": _schema({"limit": LIMIT}),
    "whatsapp_search_chats": _schema({"query": TEXT, "limit": LIMIT}, ("query",)),
    "whatsapp_open_chat": _schema({"chat_id": CHAT}, ("chat_id",)),
    "whatsapp_read_messages": _schema({"chat_id": CHAT, "limit": LIMIT}, ("chat_id",)),
    "whatsapp_search_messages": _schema(
        {"chat_id": CHAT, "query": TEXT, "limit": LIMIT}, ("query",),
    ),
    "whatsapp_read_history": _schema(
        {"chat_id": CHAT, "limit": LIMIT, "max_scroll_iterations": {
            "type": "integer", "minimum": 1, "maximum": 64,
        }}, ("chat_id",),
    ),
    "whatsapp_get_chat_info": _schema({"chat_id": CHAT}, ("chat_id",)),
    "whatsapp_get_media": _schema({"chat_id": CHAT, "message_id": MESSAGE}, ("chat_id", "message_id")),
    "whatsapp_get_document": _schema({"chat_id": CHAT, "message_id": MESSAGE}, ("chat_id", "message_id")),
    "whatsapp_get_audio": _schema({"chat_id": CHAT, "message_id": MESSAGE}, ("chat_id", "message_id")),
    "whatsapp_send_message": _schema({
        "execution_id": {"type": "string", "pattern": r"^waexec_[a-f0-9]{24}$"},
        "approval_request_id": {"type": "string", "pattern": r"^apr_[A-Z2-9]{8}$"},
        "chat_id": CHAT, "chat_title": TEXT, "body": TEXT,
        "draft_hash": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
    }, ("execution_id", "approval_request_id", "chat_id", "chat_title", "body", "draft_hash")),
    "whatsapp_reply_message": _schema({
        "execution_id": {"type": "string", "pattern": r"^waexec_[a-f0-9]{24}$"},
        "approval_request_id": {"type": "string", "pattern": r"^apr_[A-Z2-9]{8}$"},
        "chat_id": CHAT, "chat_title": TEXT, "message_id": MESSAGE, "body": TEXT,
        "draft_hash": {"type": "string", "pattern": r"^[a-f0-9]{64}$"},
    }, ("execution_id", "approval_request_id", "chat_id", "chat_title", "message_id", "body", "draft_hash")),
}


class WhatsAppMCPServer:
    def __init__(
        self,
        provider: WhatsAppWebReadOnly,
        *,
        writer: WhatsAppBrowserWriter | None = None,
        approval_store: DomainApprovalStore | None = None,
    ) -> None:
        self.provider = provider
        self.writer = writer
        self.approval_store = approval_store

    def list_tools(self) -> list[dict[str, Any]]:
        return [{
            "name": name,
            "description": "Semantic WhatsApp work-profile operation.",
            "inputSchema": schema,
        } for name, schema in TOOLS.items()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS:
            return _error("POLICY_DENIED")
        invalid = _validate(arguments, TOOLS[name])
        if invalid:
            return _error(invalid)
        try:
            result = self._dispatch(name, dict(arguments))
        except Exception:
            return _error("SOURCE_UNAVAILABLE")
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "structuredContent": result,
            "isError": not bool(result.get("ok", True)),
        }

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 100)
        if name == "whatsapp_list_chats":
            rows = self.provider.list_chats()[:limit]
            return _read("list_chats", {"results": _dump(rows)})
        if name == "whatsapp_search_chats":
            rows = self.provider.search_chats(str(args["query"]))[:limit]
            return _read("search_chats", {"results": _dump(rows)})
        if name == "whatsapp_open_chat":
            result = self.provider.open_chat(str(args["chat_id"]))
            return _read("open_chat", result)
        if name == "whatsapp_read_messages":
            result = self.provider.read_messages(str(args["chat_id"]), limit=limit)
            return _read("read_messages", result.model_dump(mode="json"))
        if name == "whatsapp_search_messages":
            result = self.provider.search_messages(
                str(args["query"]), chat_ref=str(args.get("chat_id") or ""), limit=limit,
            )
            return _read("search_messages", result.model_dump(mode="json"))
        if name == "whatsapp_read_history":
            result = self.provider.read_history(
                str(args["chat_id"]), limit=limit,
                max_scroll_iterations=int(args.get("max_scroll_iterations") or 8),
            )
            return _read("read_history", result.model_dump(mode="json"))
        if name == "whatsapp_get_chat_info":
            return _read("get_chat_info", self.provider.get_chat_info(str(args["chat_id"])))
        if name in {"whatsapp_get_media", "whatsapp_get_document", "whatsapp_get_audio"}:
            rows = self.provider.get_media(str(args["chat_id"]), str(args["message_id"]))
            kind = "document" if name.endswith("document") else "audio" if name.endswith("audio") else ""
            if kind:
                rows = tuple(item for item in rows if item.kind == kind)
            return _read(name.removeprefix("whatsapp_"), {"results": _dump(rows)})
        return self._approved_write(name, args)

    def _approved_write(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.writer is None or self.approval_store is None:
            return {"ok": False, "status": "SOURCE_UNAVAILABLE", "side_effects": 0}
        request_id = str(args["approval_request_id"])
        row = self.approval_store.get_request(request_id)
        scope = (row or {}).get("scope") or {}
        expected_action = "whatsapp_reply" if name == "whatsapp_reply_message" else "whatsapp_send"
        bindings = (
            row is not None
            and row.get("status") == "executing"
            and row.get("consumed_at") is None
            and int(row.get("expires_at") or 0) > int(time.time())
            and row.get("action") == expected_action
            and scope.get("action") == expected_action
            and scope.get("execution_id") == args["execution_id"]
            and scope.get("artifact_sha256") == args["draft_hash"]
            and scope.get("chat_id") == args["chat_id"]
            and scope.get("chat_title") == args["chat_title"]
            and scope.get("body") == args["body"]
            and scope.get("body_sha256") == hashlib.sha256(str(args["body"]).encode()).hexdigest()
            and (name != "whatsapp_reply_message" or scope.get("reply_to_message_id") == args["message_id"])
        )
        if not bindings:
            return {"ok": False, "status": "APPROVAL_INVALID", "side_effects": 0}
        if name == "whatsapp_reply_message":
            return self.writer.reply_message(
                chat_id=str(args["chat_id"]), message_id=str(args["message_id"]),
                chat_title=str(args["chat_title"]),
                body=str(args["body"]), execution_id=str(args["execution_id"]),
            )
        return self.writer.send_message(
            chat_id=str(args["chat_id"]), body=str(args["body"]),
            chat_title=str(args["chat_title"]),
            execution_id=str(args["execution_id"]),
        )


def _validate(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    if not isinstance(arguments, Mapping):
        return "POLICY_DENIED"
    properties = schema["properties"]
    if set(arguments) - set(properties) or set(schema.get("required") or ()) - set(arguments):
        return "POLICY_DENIED"
    for name, value in arguments.items():
        spec = properties[name]
        if spec.get("type") == "string":
            if not isinstance(value, str):
                return "POLICY_DENIED"
            if len(value) < int(spec.get("minLength", 0)) or len(value) > int(spec.get("maxLength", 1_000_000)):
                return "POLICY_DENIED"
            pattern = spec.get("pattern")
            if pattern:
                import re
                if not re.fullmatch(str(pattern), value):
                    return "POLICY_DENIED"
        if spec.get("type") == "integer":
            if not isinstance(value, int) or not int(spec.get("minimum", value)) <= value <= int(spec.get("maximum", value)):
                return "POLICY_DENIED"
    return ""


def _dump(rows: Any) -> list[dict[str, Any]]:
    return [
        item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        for item in rows
    ]


def _read(operation: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": True, "operation": operation, **dict(payload),
        "side_effects": 0, "writes": 0, "sends": 0,
    }


def _error(code: str) -> dict[str, Any]:
    payload = {"ok": False, "status": code, "side_effects": 0}
    return {
        "content": [{"type": "text", "text": code}],
        "structuredContent": payload, "isError": True,
    }


def _response(request: Mapping[str, Any], server: WhatsAppMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "ralf-whatsapp-work", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": server.list_tools()}
    elif method == "tools/call":
        params = request.get("params") or {}
        result = server.call(str(params.get("name") or ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method_not_found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    provider = WhatsAppWebReadOnly.from_environment()
    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy) if policy.enabled and policy.db_path else None
    writer = WhatsAppBrowserWriter.from_environment() if store is not None else None
    server = WhatsAppMCPServer(provider, writer=writer, approval_store=store)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            response = _response(request, server)
        except Exception:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal_error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
