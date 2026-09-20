#!/usr/bin/env python3
from __future__ import annotations

"""Semantic MCP surface for Bot-tazzi MD/Goodify automation."""

import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.md_goodify import (
    DEFAULT_FORWARD_TO,
    DEFAULT_NONPROFIT,
    decode_qr_image,
    parse_md_goodify_qr,
    probe_public_flow,
    process_goodify_mailbox,
)
from src.mcp_transport import MCP_PROTOCOL_VERSION

TEXT = {"type": "string", "minLength": 1, "maxLength": 4096}
EMAIL = {"type": "string", "pattern": r"^[^\s@]+@[^\s@]+\.[^\s@]+$", "maxLength": 320}
PATH = {"type": "string", "minLength": 1, "maxLength": 1024}


def _schema(properties: Mapping[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": dict(properties), "required": list(required), "additionalProperties": False}


TOOLS: dict[str, dict[str, Any]] = {
    "md_goodify_parse_qr": _schema({"payload": TEXT}, ("payload",)),
    "md_goodify_decode_qr_image": _schema({"image_path": PATH}, ("image_path",)),
    "md_goodify_probe_public_flow": _schema({"payload": TEXT}, ("payload",)),
    "md_goodify_prepare_donation": _schema({
        "payload": TEXT,
        "nonprofit": {"type": "string", "minLength": 1, "maxLength": 200, "default": DEFAULT_NONPROFIT},
    }, ("payload",)),
    "md_goodify_poll_mail": _schema({
        "account": EMAIL,
        "forward_to": {**EMAIL, "default": DEFAULT_FORWARD_TO},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50},
    }, ("account",)),
}


class MdGoodifyMCPServer:
    def list_tools(self) -> list[dict[str, Any]]:
        descriptions = {
            "md_goodify_parse_qr": "Validate and parse an MD/Goodify QR payload without side effects.",
            "md_goodify_decode_qr_image": "Decode an MD/Goodify QR from the configured Bot-tazzi spool directory.",
            "md_goodify_probe_public_flow": "Follow only allow-listed public HTTPS redirects and describe the Goodify web flow.",
            "md_goodify_prepare_donation": "Prepare a donation routing plan for a named nonprofit; does not submit forms.",
            "md_goodify_poll_mail": "Process allow-listed MD/Goodify emails, narrowly forward them, and notify Telegram on an already reported win.",
        }
        return [{"name": name, "description": descriptions[name], "inputSchema": schema} for name, schema in TOOLS.items()]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name not in TOOLS or _validate(arguments, TOOLS[name]):
            return _error("POLICY_DENIED")
        try:
            if name == "md_goodify_parse_qr":
                qr = parse_md_goodify_qr(str(arguments["payload"]))
                result = {"ok": True, "operation": "parse_qr", **qr.__dict__, "side_effects": 0}
            elif name == "md_goodify_decode_qr_image":
                qr = decode_qr_image(str(arguments["image_path"]))
                result = {"ok": True, "operation": "decode_qr_image", **qr.__dict__, "side_effects": 0}
            elif name == "md_goodify_probe_public_flow":
                qr = parse_md_goodify_qr(str(arguments["payload"]))
                result = {"ok": True, "operation": "probe_public_flow", **probe_public_flow(qr)}
            elif name == "md_goodify_prepare_donation":
                qr = parse_md_goodify_qr(str(arguments["payload"]))
                nonprofit = str(arguments.get("nonprofit") or DEFAULT_NONPROFIT).strip()
                result = {
                    "ok": True,
                    "operation": "prepare_donation",
                    "status": "READY_FOR_PUBLIC_FLOW_VALIDATION",
                    "qr_fingerprint": qr.fingerprint,
                    "public_url": qr.url,
                    "nonprofit": nonprofit,
                    "selection_target": nonprofit,
                    "submission_performed": False,
                    "side_effects": 0,
                    "boundary": "No app-auth bypass or protected endpoint. Submit only after a live QR proves a public supported form.",
                }
            else:
                result = process_goodify_mailbox(
                    account=str(arguments["account"]),
                    forward_to=str(arguments.get("forward_to") or DEFAULT_FORWARD_TO),
                    limit=int(arguments.get("limit") or 50),
                )
                result["operation"] = "poll_mail"
                result["side_effects"] = sum(bool(e.get("forwarded")) for e in result.get("events", []))
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result, "isError": False}
        except Exception as exc:
            return _error(str(exc)[:200] or "SOURCE_UNAVAILABLE")


def _validate(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> str:
    if not isinstance(arguments, Mapping):
        return "POLICY_DENIED"
    properties = schema["properties"]
    if set(arguments) - set(properties) or set(schema.get("required") or ()) - set(arguments):
        return "POLICY_DENIED"
    for key, value in arguments.items():
        spec = properties[key]
        if spec.get("type") == "string":
            if not isinstance(value, str):
                return "POLICY_DENIED"
            if len(value) < int(spec.get("minLength", 0)) or len(value) > int(spec.get("maxLength", 1_000_000)):
                return "POLICY_DENIED"
            pattern = spec.get("pattern")
            if pattern and not __import__("re").fullmatch(str(pattern), value):
                return "POLICY_DENIED"
        elif spec.get("type") == "integer":
            if not isinstance(value, int):
                return "POLICY_DENIED"
            if value < int(spec.get("minimum", value)) or value > int(spec.get("maximum", value)):
                return "POLICY_DENIED"
    return ""


def _error(code: str) -> dict[str, Any]:
    payload = {"ok": False, "status": code, "side_effects": 0}
    return {"content": [{"type": "text", "text": code}], "structuredContent": payload, "isError": True}


def _response(request: Mapping[str, Any], server: MdGoodifyMCPServer) -> dict[str, Any] | None:
    method = request.get("method")
    if method == "notifications/initialized":
        return None
    request_id = request.get("id")
    if method == "initialize":
        result = {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "bot-tazzi-md-goodify", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": server.list_tools()}
    elif method == "tools/call":
        params = request.get("params") or {}
        result = server.call(str(params.get("name") or ""), params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method_not_found"}}
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    server = MdGoodifyMCPServer()
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
