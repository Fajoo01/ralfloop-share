from __future__ import annotations

import base64
import json
import os
from email.message import EmailMessage
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

TOOL_NAME = "upsert_email_draft"
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def tool_definition() -> dict[str, Any]:
    return {
        "name": TOOL_NAME,
        "description": "Create or update one Gmail draft. Never sends mail.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "draftId": {"type": "string"},
                "cc": {"type": "string"},
                "bcc": {"type": "string"},
                "threadId": {"type": "string"},
            },            "required": ["email", "to", "subject", "body"],
            "additionalProperties": False,
        },
    }


def _credential_path(email: str) -> Path:
    safe = email.replace("/", "").replace("\\", "")
    slug = safe.replace("@", "_at_").replace(".", "_dot_")
    data_home = Path(os.getenv("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return data_home / "google-workspace-mcp" / "credentials" / f"{slug}.json"


def _access_token(email: str) -> str:
    cached = _TOKEN_CACHE.get(email)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    cred = json.loads(_credential_path(email).read_text(encoding="utf-8"))
    payload = urllib.parse.urlencode({
        "client_id": cred["client_id"],
        "client_secret": cred["client_secret"],
        "refresh_token": cred["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        data = json.load(response)
    token = str(data["access_token"])
    _TOKEN_CACHE[email] = (token, time.time() + int(data.get("expires_in") or 3600))
    return token


def _raw_message(args: dict[str, Any]) -> str:
    message = EmailMessage()
    message["To"] = str(args["to"])
    message["Subject"] = str(args["subject"])
    if str(args.get("cc") or "").strip():
        message["Cc"] = str(args["cc"])
    if str(args.get("bcc") or "").strip():
        message["Bcc"] = str(args["bcc"])
    message.set_content(str(args["body"]))
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


def upsert_draft(args: dict[str, Any]) -> dict[str, Any]:
    email = str(args.get("email") or "").strip()
    recipient = str(args.get("to") or "").strip()
    subject = str(args.get("subject") or "")
    body = str(args.get("body") or "")
    if not email or "@" not in recipient or not body.strip():
        raise ValueError("draft_arguments_invalid")
    draft_id = str(args.get("draftId") or "").strip()
    message: dict[str, Any] = {"raw": _raw_message(args)}
    thread_id = str(args.get("threadId") or "").strip()
    if thread_id:
        message["threadId"] = thread_id
    url = "https://gmail.googleapis.com/gmail/v1/users/me/drafts"
    method = "POST"
    if draft_id:
        url += "/" + urllib.parse.quote(draft_id, safe="")
        method = "PUT"
    req = urllib.request.Request(
        url,
        data=json.dumps({"message": message}).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + _access_token(email),
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        data = json.load(response)
    returned_id = str(data.get("id") or "")
    if not returned_id:
        raise RuntimeError("gmail_draft_confirmation_missing")
    if draft_id and returned_id != draft_id:
        raise RuntimeError("gmail_draft_id_changed")
    return {
        "status": "draft_updated" if draft_id else "draft_created",
        "draftId": returned_id,
        "messageId": str((data.get("message") or {}).get("id") or ""),
        "sent": False,
    }


def handle_tool_call(
    line: bytes, *, caller_uid: int, write_uid: int | None
) -> bytes | None:
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    params = request.get("params") if isinstance(request, dict) else None
    if request.get("method") != "tools/call" or not isinstance(params, dict):
        return None
    if str(params.get("name") or "") != TOOL_NAME:
        return None
    if write_uid is None or caller_uid != write_uid:
        payload = {
            "jsonrpc": "2.0", "id": request.get("id"),
            "error": {
                "code": -32003,
                "message": "gmail_draft_write_requires_backend_uid",
            },
        }
        return json.dumps(payload, separators=(",", ":")).encode() + b"\n"
    try:
        result = upsert_draft(dict(params.get("arguments") or {}))
        payload = {
            "structuredContent": result,
            "content": [{"type": "text", "text": json.dumps(result)}],
            "isError": False,
        }
        response = {"jsonrpc": "2.0", "id": request.get("id"), "result": payload}
    except Exception as exc:
        response = {
            "jsonrpc": "2.0", "id": request.get("id"),
            "error": {"code": -32004, "message": type(exc).__name__},
        }
    return json.dumps(response, separators=(",", ":")).encode() + b"\n"
