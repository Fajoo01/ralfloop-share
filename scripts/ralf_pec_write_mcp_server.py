#!/usr/bin/env python3
from __future__ import annotations

"""Approval-bound MCP server for PEC writes. Reader remains separate."""

import json
from pathlib import Path
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.pec_smtp_writer import PecSmtpConfig, PecSmtpWriter, PecWriterError
from src.mcp_transport import MCP_PROTOCOL_VERSION

TEXT = {"type": "string", "minLength": 1, "maxLength": 100000}
RECIPIENT = {"type": "string", "minLength": 3, "maxLength": 320}
SUBJECT = {"type": "string", "minLength": 1, "maxLength": 500}
PATHS = {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 4096}, "maxItems": 20}
ID = {"type": "string", "minLength": 1, "maxLength": 240}
TOOLS: dict[str, tuple[dict[str, Any], list[str]]] = {
    "pec_writer_preflight": ({}, []),
    "pec_prepare_send": ({"recipient": RECIPIENT, "subject": SUBJECT, "body": TEXT, "attachment_paths": PATHS, "requested_by": {"type": "string", "minLength": 1, "maxLength": 200}}, ["recipient", "subject", "body"]),
    "pec_send_approved": ({"approval_request_id": ID, "recipient": RECIPIENT, "subject": SUBJECT, "body": TEXT, "attachment_paths": PATHS}, ["approval_request_id", "recipient", "subject", "body"]),
}


class PecWriteMCPServer:
    def __init__(self, writer: PecSmtpWriter) -> None:
        self.writer = writer

    def list_tools(self) -> list[dict[str, Any]]:
        descriptions = {
            "pec_writer_preflight": "Verify PEC SMTP TLS/authentication without sending or modifying the mailbox.",
            "pec_prepare_send": "Validate a PEC draft and create a hash-bound approval request; does not send.",
            "pec_send_approved": "Send exactly one PEC only when the stored approval matches the exact draft.",
        }
        return [{"name": name, "description": descriptions[name], "inputSchema": {"type": "object", "properties": properties, "required": required, "additionalProperties": False}} for name, (properties, required) in TOOLS.items()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        contract = TOOLS.get(name)
        if contract is None or not isinstance(arguments, Mapping):
            return _error("POLICY_DENIED")
        properties, required = contract
        if not set(arguments) <= set(properties) or not set(required) <= set(arguments):
            return _error("POLICY_DENIED")
        try:
            if name == "pec_writer_preflight":
                return _result(self.writer.preflight())
            if name == "pec_prepare_send":
                return _result(self.writer.prepare_send(recipient=str(arguments["recipient"]), subject=str(arguments["subject"]), body=str(arguments["body"]), attachment_paths=tuple(str(x) for x in arguments.get("attachment_paths") or ()), requested_by=str(arguments.get("requested_by") or "bot-tazzi")))
            return _result(self.writer.send_approved(approval_request_id=str(arguments["approval_request_id"]), recipient=str(arguments["recipient"]), subject=str(arguments["subject"]), body=str(arguments["body"]), attachment_paths=tuple(str(x) for x in arguments.get("attachment_paths") or ())))
        except PecWriterError as exc:
            return _error(str(exc))
        except Exception:
            return _error("PEC_WRITE_UNAVAILABLE")


def _result(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(payload)
    return {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False, default=str)}], "structuredContent": data, "isError": not bool(data.get("ok"))}


def _error(status: str) -> dict[str, Any]:
    return _result({"ok": False, "status": status, "writes": 0, "sends": 0})


def _response(request: Mapping[str, Any], server: PecWriteMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        result = {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "ralf-pec-write", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": server.list_tools()}
    elif method == "tools/call":
        params = request.get("params")
        result = _error("POLICY_DENIED") if not isinstance(params, Mapping) else server.call(str(params.get("name") or ""), params.get("arguments", {}))
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method_not_found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    store = DomainApprovalStore()
    server = PecWriteMCPServer(PecSmtpWriter(PecSmtpConfig.from_environment(), store=store))
    for line in sys.stdin:
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                raise ValueError("request_not_object")
            response = _response(request, server)
        except Exception:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": "internal_error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
