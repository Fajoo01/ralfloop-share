#!/usr/bin/env python3
from __future__ import annotations

"""Read-only Mailchimp Marketing API MCP server for Ralfloop."""

import base64
import hashlib
import html
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp_transport import MCP_PROTOCOL_VERSION
from ralfloop_agent.domains.domain_approval import effective_approval_status, scope_digest
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.mailchimp_campaign import (
    CREATE_ACTION, SEND_ACTION, SUBSCRIBE_ACTION,
    build_mailchimp_campaign_create_scope, build_mailchimp_campaign_send_scope,
    build_mailchimp_member_subscribe_scope, campaign_fingerprint,
)


def _schema(
    properties: Mapping[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": False,
    }


COUNT = {
    "type": "integer",
    "minimum": 1,
    "maximum": 1000,
}

OFFSET = {
    "type": "integer",
    "minimum": 0,
    "maximum": 1_000_000,
}

RESOURCE_ID = {
    "type": "string",
    "pattern": r"^[A-Za-z0-9_-]{1,128}$",
}

SUBSCRIBER_HASH = {
    "type": "string",
    "pattern": r"^[a-fA-F0-9]{32}$",
}

MEMBER_STATUS = {
    "type": "string",
    "enum": [
        "subscribed",
        "unsubscribed",
        "cleaned",
        "pending",
        "transactional",
        "archived",
    ],
}

DIGEST = {"type": "string", "pattern": r"^[a-f0-9]{64}$"}
EXECUTION_ID = {"type": "string", "pattern": r"^mc(?:create|send|subscribe)_[a-f0-9]{24}$"}
REQUEST_ID = {"type": "string", "pattern": r"^apr_[A-Za-z0-9_-]{8,128}$"}
SHORT_TEXT = {"type": "string", "minLength": 1, "maxLength": 255}
BODY_TEXT = {"type": "string", "minLength": 1, "maxLength": 200000}
EMAIL_ADDRESS = {"type": "string", "minLength": 3, "maxLength": 320, "pattern": r"^[^@\s]+@[^@\s]+\.[^@\s]+$"}
NAME_TEXT = {"type": "string", "maxLength": 255}


TOOLS: dict[str, dict[str, Any]] = {
    "mailchimp_ping": _schema({}),
    "mailchimp_list_audiences": _schema({
        "count": COUNT,
        "offset": OFFSET,
    }),
    "mailchimp_list_campaigns": _schema({
        "count": COUNT,
        "offset": OFFSET,
    }),
    "mailchimp_get_campaign_content": _schema({
        "campaign_id": RESOURCE_ID,
    }, ("campaign_id",)),
    "mailchimp_list_members": _schema({
        "list_id": RESOURCE_ID,
        "count": COUNT,
        "offset": OFFSET,
        "status": MEMBER_STATUS,
    }, ("list_id",)),
    "mailchimp_list_segments": _schema({
        "list_id": RESOURCE_ID,
        "count": COUNT,
        "offset": OFFSET,
    }, ("list_id",)),
    "mailchimp_list_tags": _schema({
        "list_id": RESOURCE_ID,
        "name": {
            "type": "string",
            "minLength": 1,
            "maxLength": 255,
        },
    }, ("list_id",)),
    "mailchimp_list_member_tags": _schema({
        "list_id": RESOURCE_ID,
        "subscriber_hash": SUBSCRIBER_HASH,
    }, ("list_id", "subscriber_hash")),
    "mailchimp_create_approved_campaign": _schema({
        "approval_request_id": REQUEST_ID, "execution_id": EXECUTION_ID,
        "draft_id": SHORT_TEXT, "draft_version": {"type": "integer", "minimum": 1, "maximum": 1000000},
        "payload_digest": DIGEST, "source_draft_sha256": DIGEST,
        "list_id": RESOURCE_ID, "subject": SHORT_TEXT, "from_name": SHORT_TEXT,
        "reply_to": SHORT_TEXT, "preheader": {"type": "string", "maxLength": 255},
        "html_body": {"type": "string", "minLength": 1, "maxLength": 1000000},
        "body_text": BODY_TEXT, "cta_label": {"type": "string", "maxLength": 255},
        "cta_target": {"type": "string", "maxLength": 2048},
        "internal_title": {"type": "string", "maxLength": 255},
        "provider_identity": SHORT_TEXT,
    }, ("approval_request_id", "execution_id", "draft_id", "draft_version", "payload_digest",
        "source_draft_sha256", "list_id", "subject", "from_name", "reply_to", "preheader",
        "html_body", "body_text", "cta_label", "cta_target", "internal_title", "provider_identity")),
    "mailchimp_send_approved_campaign": _schema({
        "approval_request_id": REQUEST_ID, "execution_id": EXECUTION_ID,
        "campaign_id": RESOURCE_ID, "list_id": RESOURCE_ID,
        "provider_campaign_sha256": DIGEST, "subject": SHORT_TEXT,
        "from_name": SHORT_TEXT, "reply_to": SHORT_TEXT,
        "content_sha256": DIGEST, "html_sha256": DIGEST, "provider_identity": SHORT_TEXT,
    }, ("approval_request_id", "execution_id", "campaign_id", "list_id",
        "provider_campaign_sha256", "subject", "from_name", "reply_to",
        "content_sha256", "html_sha256", "provider_identity")),
    "mailchimp_subscribe_approved_member": _schema({
        "approval_request_id": REQUEST_ID, "execution_id": EXECUTION_ID,
        "list_id": RESOURCE_ID, "email_address": EMAIL_ADDRESS,
        "first_name": NAME_TEXT, "last_name": NAME_TEXT,
        "provider_identity": SHORT_TEXT,
    }, ("approval_request_id", "execution_id", "list_id", "email_address",
        "first_name", "last_name", "provider_identity")),
}


class MailchimpAPIError(RuntimeError):
    def __init__(
        self,
        status: str,
        *,
        http_status: int | None = None,
        detail: str = "",
    ) -> None:
        super().__init__(status)
        self.status = status
        self.http_status = http_status
        self.detail = detail[:1000]


class MailchimpClient:
    def __init__(
        self,
        api_key: str,
        server_prefix: str,
        *,
        timeout: float = 20.0,
    ) -> None:
        self.api_key = api_key.strip()
        self.server_prefix = server_prefix.strip()
        self.timeout = timeout

        if not self.api_key:
            raise ValueError("mailchimp_api_key_missing")
        if not self.server_prefix:
            raise ValueError("mailchimp_server_prefix_missing")

        if not all(
            ch.isalnum() or ch == "-"
            for ch in self.server_prefix
        ):
            raise ValueError("mailchimp_server_prefix_invalid")

        self.base_url = (
            f"https://{self.server_prefix}.api.mailchimp.com/3.0"
        )

    @classmethod
    def from_environment(cls) -> "MailchimpClient":
        api_key = os.getenv("MAILCHIMP_API_KEY", "").strip()

        server_prefix = os.getenv(
            "MAILCHIMP_SERVER_PREFIX",
            "",
        ).strip()

        # Le API key Mailchimp normalmente terminano con
        # "-<datacenter>", ad esempio "-us21".
        # L'env esplicita ha comunque precedenza.
        if not server_prefix and "-" in api_key:
            candidate = api_key.rsplit("-", 1)[-1].strip()
            if candidate:
                server_prefix = candidate

        timeout = float(
            os.getenv("MAILCHIMP_API_TIMEOUT", "20")
        )

        return cls(
            api_key,
            server_prefix,
            timeout=timeout,
        )

    def _request(
        self,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        method: str = "GET",
        body: Mapping[str, Any] | None = None,
        empty_ok: bool = False,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"

        if query:
            clean_query = {
                key: value
                for key, value in query.items()
                if value is not None
            }
            if clean_query:
                url += "?" + urlencode(clean_query)

        credentials = base64.b64encode(
            f"ralf:{self.api_key}".encode("utf-8")
        ).decode("ascii")

        encoded_body = None if body is None else json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        request = Request(
            url,
            data=encoded_body,
            headers={
                "Authorization": f"Basic {credentials}",
                "Accept": "application/json",
                "User-Agent": "ralfloop-mailchimp-mcp/1",
                **({"Content-Type": "application/json"} if encoded_body is not None else {}),
            },
            method=method,
        )

        try:
            with urlopen(
                request,
                timeout=self.timeout,
            ) as response:
                raw = response.read(8 * 1024 * 1024)

        except HTTPError as exc:
            try:
                raw = exc.read(1024 * 1024)
                body = json.loads(
                    raw.decode("utf-8", errors="replace")
                )
                detail = str(
                    body.get("detail")
                    or body.get("title")
                    or ""
                )
            except Exception:
                detail = ""

            if exc.code in {401, 403}:
                status = "AUTH_REQUIRED"
            elif exc.code == 404:
                status = "NOT_FOUND"
            elif exc.code == 429:
                status = "RATE_LIMITED"
            else:
                status = "REMOTE_ERROR"

            raise MailchimpAPIError(
                status,
                http_status=exc.code,
                detail=detail,
            ) from exc

        except (URLError, TimeoutError, OSError) as exc:
            raise MailchimpAPIError(
                "SOURCE_UNAVAILABLE"
            ) from exc

        if empty_ok and not raw:
            return {}

        try:
            payload = json.loads(
                raw.decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MailchimpAPIError(
                "MALFORMED_RESPONSE"
            ) from exc

        if not isinstance(payload, dict):
            raise MailchimpAPIError(
                "MALFORMED_RESPONSE"
            )

        return payload

    @staticmethod
    def _campaign_view(info: Mapping[str, Any], content: Mapping[str, Any]) -> dict[str, Any]:
        settings = info.get("settings") if isinstance(info.get("settings"), Mapping) else {}
        recipients = info.get("recipients") if isinstance(info.get("recipients"), Mapping) else {}
        plain = str(content.get("plain_text") or "")
        html_content = str(content.get("html") or "")
        return {
            "campaign_id": str(info.get("id") or ""),
            "list_id": str(recipients.get("list_id") or ""),
            "subject": str(settings.get("subject_line") or ""),
            "from_name": str(settings.get("from_name") or ""),
            "reply_to": str(settings.get("reply_to") or ""),
            "content_sha256": hashlib.sha256(plain.encode()).hexdigest(),
            "html_sha256": hashlib.sha256(html_content.encode()).hexdigest(),
            "sent": str(info.get("status") or "") in {"sending", "sent"},
            "provider_status": str(info.get("status") or ""),
        }

    def get_campaign(self, campaign_id: str) -> dict[str, Any]:
        return self._campaign_view(
            self._request(f"campaigns/{campaign_id}"),
            self._request(f"campaigns/{campaign_id}/content"),
        )

    def get_campaign_content(self, campaign_id: str) -> dict[str, Any]:
        info = self._request(f"campaigns/{campaign_id}")
        content = self._request(f"campaigns/{campaign_id}/content")
        settings = info.get("settings") if isinstance(info.get("settings"), Mapping) else {}
        recipients = info.get("recipients") if isinstance(info.get("recipients"), Mapping) else {}
        html_content = str(content.get("html") or "")
        plain_text = str(content.get("plain_text") or "")
        return {
            "campaign_id": str(info.get("id") or ""),
            "list_id": str(recipients.get("list_id") or ""),
            "type": str(info.get("type") or ""),
            "status": str(info.get("status") or ""),
            "create_time": info.get("create_time"),
            "send_time": info.get("send_time"),
            "subject": str(settings.get("subject_line") or ""),
            "preheader": str(settings.get("preview_text") or ""),
            "title": str(settings.get("title") or ""),
            "from_name": str(settings.get("from_name") or ""),
            "reply_to": str(settings.get("reply_to") or ""),
            "template_id": settings.get("template_id"),
            "content_type": info.get("content_type"),
            "archive_url": str(info.get("archive_url") or ""),
            "html": html_content,
            "plain_text": plain_text,
            "html_sha256": hashlib.sha256(html_content.encode()).hexdigest(),
            "plain_text_sha256": hashlib.sha256(plain_text.encode()).hexdigest(),
        }

    def create_campaign(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        created = self._request("campaigns", method="POST", body={
            "type": "regular", "recipients": {"list_id": scope["list_id"]},
            "settings": {
                "subject_line": scope["subject"], "preview_text": scope["preheader"],
                "title": scope["internal_title"], "from_name": scope["from_name"],
                "reply_to": scope["reply_to"],
            },
        })
        campaign_id = str(created.get("id") or "")
        if not campaign_id:
            raise MailchimpAPIError("MALFORMED_RESPONSE")
        self._request(
            f"campaigns/{campaign_id}/content", method="PUT",
            body={"plain_text": scope["body_text"], "html": scope["html_body"]},
        )
        return self.get_campaign(campaign_id)

    def send_campaign(self, campaign_id: str) -> dict[str, Any]:
        self._request(
            f"campaigns/{campaign_id}/actions/send", method="POST", body={}, empty_ok=True,
        )
        return self.get_campaign(campaign_id)

    def ping(self) -> dict[str, Any]:
        payload = self._request("ping")

        return {
            "health_status": str(
                payload.get("health_status") or ""
            ),
        }

    def list_audiences(
        self,
        *,
        count: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = self._request(
            "lists",
            query={
                "count": count,
                "offset": offset,
            },
        )

        rows = []

        for item in payload.get("lists") or []:
            if not isinstance(item, Mapping):
                continue

            stats = item.get("stats")
            if not isinstance(stats, Mapping):
                stats = {}

            rows.append({
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or ""),
                "date_created": item.get("date_created"),
                "member_count": stats.get("member_count"),
                "unsubscribe_count": stats.get(
                    "unsubscribe_count"
                ),
                "cleaned_count": stats.get(
                    "cleaned_count"
                ),
                "campaign_count": stats.get(
                    "campaign_count"
                ),
            })

        return {
            "results": rows,
            "total_items": int(
                payload.get("total_items") or 0
            ),
            "count": count,
            "offset": offset,
        }

    def list_campaigns(
        self,
        *,
        count: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = self._request(
            "campaigns",
            query={
                "count": count,
                "offset": offset,
            },
        )

        rows = []

        for item in payload.get("campaigns") or []:
            if not isinstance(item, Mapping):
                continue

            settings = item.get("settings")
            if not isinstance(settings, Mapping):
                settings = {}

            recipients = item.get("recipients")
            if not isinstance(recipients, Mapping):
                recipients = {}

            rows.append({
                "id": str(item.get("id") or ""),
                "type": str(item.get("type") or ""),
                "status": str(item.get("status") or ""),
                "create_time": item.get("create_time"),
                "send_time": item.get("send_time"),
                "subject_line": str(
                    settings.get("subject_line") or ""
                ),
                "preview_text": str(
                    settings.get("preview_text") or ""
                ),
                "title": str(
                    settings.get("title") or ""
                ),
                "list_id": str(
                    recipients.get("list_id") or ""
                ),
                "recipient_count": recipients.get(
                    "recipient_count"
                ),
            })

        return {
            "results": rows,
            "total_items": int(
                payload.get("total_items") or 0
            ),
            "count": count,
            "offset": offset,
        }

    def list_members(
        self,
        list_id: str,
        *,
        count: int = 20,
        offset: int = 0,
        status: str | None = None,
    ) -> dict[str, Any]:
        payload = self._request(
            f"lists/{list_id}/members",
            query={"count": count, "offset": offset, "status": status},
        )
        rows = []
        for item in payload.get("members") or []:
            if not isinstance(item, Mapping):
                continue
            merge_fields = item.get("merge_fields")
            tags = item.get("tags")
            rows.append({
                "id": str(item.get("id") or ""),
                "email_address": str(item.get("email_address") or ""),
                "status": str(item.get("status") or ""),
                "merge_fields": dict(merge_fields) if isinstance(merge_fields, Mapping) else {},
                "tags": [dict(tag) for tag in tags if isinstance(tag, Mapping)] if isinstance(tags, list) else [],
                "timestamp_signup": item.get("timestamp_signup"),
                "last_changed": item.get("last_changed"),
            })
        return {
            "results": rows,
            "total_items": int(payload.get("total_items") or 0),
            "count": count,
            "offset": offset,
        }

    def list_segments(
        self,
        list_id: str,
        *,
        count: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = self._request(
            f"lists/{list_id}/segments",
            query={"count": count, "offset": offset},
        )
        rows = []
        for item in payload.get("segments") or []:
            if not isinstance(item, Mapping):
                continue
            rows.append({
                "id": item.get("id"),
                "name": str(item.get("name") or ""),
                "member_count": item.get("member_count"),
                "type": str(item.get("type") or ""),
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
            })
        return {
            "results": rows,
            "total_items": int(payload.get("total_items") or 0),
            "count": count,
            "offset": offset,
        }

    def list_tags(
        self,
        list_id: str,
        *,
        name: str | None = None,
    ) -> dict[str, Any]:
        payload = self._request(
            f"lists/{list_id}/tag-search",
            query={"name": name},
        )
        rows = []
        for item in payload.get("tags") or []:
            if not isinstance(item, Mapping):
                continue
            rows.append({
                "id": item.get("id"),
                "name": str(item.get("name") or ""),
                "member_count": item.get("member_count"),
            })
        return {"results": rows}

    def list_member_tags(
        self,
        list_id: str,
        subscriber_hash: str,
    ) -> dict[str, Any]:
        payload = self._request(
            f"lists/{list_id}/members/{subscriber_hash}/tags",
        )
        rows = []
        for item in payload.get("tags") or []:
            if not isinstance(item, Mapping):
                continue
            rows.append({
                "id": item.get("id"),
                "name": str(item.get("name") or ""),
            })
        return {"results": rows}


    @staticmethod
    def _subscriber_hash(email_address: str) -> str:
        normalized = str(email_address or "").strip().casefold()
        return hashlib.md5(normalized.encode("utf-8")).hexdigest()

    def get_member(self, list_id: str, email_address: str) -> dict[str, Any] | None:
        subscriber_hash = self._subscriber_hash(email_address)
        try:
            item = self._request(f"lists/{list_id}/members/{subscriber_hash}")
        except MailchimpAPIError as exc:
            if exc.status == "NOT_FOUND":
                return None
            raise
        merge_fields = item.get("merge_fields")
        if not isinstance(merge_fields, Mapping):
            merge_fields = {}
        return {
            "list_id": list_id,
            "id": str(item.get("id") or subscriber_hash),
            "email_address": str(item.get("email_address") or "").strip().casefold(),
            "status": str(item.get("status") or ""),
            "merge_fields": dict(merge_fields),
            "last_changed": item.get("last_changed"),
        }

    def subscribe_member(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        email_address = str(scope["email_address"]).strip().casefold()
        subscriber_hash = self._subscriber_hash(email_address)
        merge_fields = {}
        if str(scope.get("first_name") or ""):
            merge_fields["FNAME"] = str(scope["first_name"])
        if str(scope.get("last_name") or ""):
            merge_fields["LNAME"] = str(scope["last_name"])
        self._request(
            f"lists/{scope['list_id']}/members/{subscriber_hash}",
            method="PUT",
            body={
                "email_address": email_address,
                "status_if_new": "subscribed",
                **({"merge_fields": merge_fields} if merge_fields else {}),
            },
        )
        observed = self.get_member(str(scope["list_id"]), email_address)
        if observed is None:
            raise MailchimpAPIError("MALFORMED_RESPONSE")
        return observed


class MailchimpMCPServer:
    def __init__(
        self,
        client: MailchimpClient | None,
        approval_store: DomainApprovalStore | None = None,
    ) -> None:
        self.client = client
        self.approval_store = approval_store

    def list_tools(self) -> list[dict[str, Any]]:
        descriptions = {
            "mailchimp_ping":
                "Check authenticated Mailchimp Marketing API availability.",
            "mailchimp_list_audiences":
                "List Mailchimp audiences without modifying remote data.",
            "mailchimp_list_campaigns":
                "List Mailchimp campaigns without modifying remote data.",
            "mailchimp_get_campaign_content":
                "Read exact Mailchimp campaign HTML and plain text without modifying it.",
            "mailchimp_list_members":
                "List minimized Mailchimp audience members without modifying them.",
            "mailchimp_list_segments":
                "List Mailchimp audience segments without modifying them.",
            "mailchimp_list_tags":
                "List or search Mailchimp audience tags without modifying them.",
            "mailchimp_list_member_tags":
                "List tags for one Mailchimp member without modifying them.",
            "mailchimp_create_approved_campaign":
                "Create one exact draft after an independently verified approval claim.",
            "mailchimp_send_approved_campaign":
                "Send one exact verified campaign after a separate approval claim.",
            "mailchimp_subscribe_approved_member":
                "Subscribe one exact audience member after approval; never silently reactivates unsubscribed or cleaned members.",
        }

        return [
            {
                "name": name,
                "description": descriptions[name],
                "inputSchema": schema,
            }
            for name, schema in TOOLS.items()
        ]

    def call(
        self,
        name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if name not in TOOLS:
            return _error("POLICY_DENIED")

        invalid = _validate(
            arguments,
            TOOLS[name],
        )

        if invalid:
            return _error(invalid)

        if self.client is None:
            return _error("AUTH_REQUIRED")

        try:
            if name in {
                "mailchimp_create_approved_campaign",
                "mailchimp_send_approved_campaign",
                "mailchimp_subscribe_approved_member",
            }:
                if self.approval_store is None:
                    return _error("APPROVAL_REQUIRED")
                request_id = str(arguments["approval_request_id"])
                action = {
                    "mailchimp_create_approved_campaign": CREATE_ACTION,
                    "mailchimp_send_approved_campaign": SEND_ACTION,
                    "mailchimp_subscribe_approved_member": SUBSCRIBE_ACTION,
                }[name]
                material = {
                    key: value for key, value in arguments.items()
                    if key not in {"approval_request_id", "execution_id"}
                }
                if action == CREATE_ACTION:
                    scope = build_mailchimp_campaign_create_scope(material)
                elif action == SEND_ACTION:
                    scope = build_mailchimp_campaign_send_scope(material)
                else:
                    scope = build_mailchimp_member_subscribe_scope(material)
                row = self.approval_store.get_request(request_id)
                if (
                    row is None or row.get("action") != action
                    or effective_approval_status(row) != "approved"
                    or row.get("scope_digest") != scope_digest(scope)
                    or arguments.get("execution_id") != scope.get("execution_id")
                ):
                    return _error("APPROVAL_INVALID")

                before = None
                try:
                    if action == CREATE_ACTION:
                        self.client._request(f"lists/{scope['list_id']}")
                    elif action == SEND_ACTION:
                        before = self.client.get_campaign(str(scope["campaign_id"]))
                        if (
                            before.get("sent")
                            or campaign_fingerprint(before) != scope["provider_campaign_sha256"]
                            or any(before.get(key) != scope.get(key) for key in (
                                "campaign_id", "list_id", "subject", "from_name", "reply_to",
                                "content_sha256", "html_sha256"
                            ))
                        ):
                            self.approval_store.mark_stale(request_id, ["mailchimp_provider_campaign_changed"])
                            return _error("DRAFT_CHANGED")
                    else:
                        before = self.client.get_member(
                            str(scope["list_id"]), str(scope["email_address"])
                        )
                        if before is not None and str(before.get("status") or "") != "subscribed":
                            self.approval_store.mark_stale(
                                request_id, ["mailchimp_member_requires_reconsent"]
                            )
                            return _error("MEMBER_REQUIRES_RECONSENT")
                except MailchimpAPIError as exc:
                    return _error(exc.status, http_status=exc.http_status, detail=exc.detail)

                claim = self.approval_store.claim_execution(request_id, action=action)
                if not claim.get("claimed"):
                    return _error(str(claim.get("status") or "APPROVAL_INVALID"))

                if action == CREATE_ACTION:
                    try:
                        observed = self.client.create_campaign(scope)
                        expected = {
                            "list_id": scope["list_id"], "subject": scope["subject"],
                            "from_name": scope["from_name"], "reply_to": scope["reply_to"],
                            "content_sha256": scope["body_sha256"],
                            "html_sha256": scope["html_sha256"], "sent": False,
                        }
                        if any(observed.get(key) != value for key, value in expected.items()):
                            raise RuntimeError("create_postcondition_mismatch")
                    except Exception:
                        failed = {"status": "CREATE_UNCERTAIN", "created": False, "retry_allowed": False}
                        self.approval_store.finish_claimed_execution(
                            request_id, action=action, success=False, result=failed,
                        )
                        return _error("CREATE_UNCERTAIN")
                    result = {"status": "executed", "state": "DRAFT", "created": True,
                              "sent": False, **observed, "retry_allowed": False}
                    self.approval_store.finish_claimed_execution(
                        request_id, action=action, success=True, result=result,
                    )
                    return _mutation("create_approved_campaign", result, writes=2, sends=0)

                if action == SUBSCRIBE_ACTION:
                    if before is not None:
                        result = {
                            "status": "already_subscribed", "state": "SUBSCRIBED",
                            "subscribed": True, "list_id": scope["list_id"],
                            "email_address": scope["email_address"],
                            "retry_allowed": False,
                        }
                        self.approval_store.finish_claimed_execution(
                            request_id, action=action, success=True, result=result,
                        )
                        return _mutation("subscribe_approved_member", result, writes=0, sends=0)
                    try:
                        observed = self.client.subscribe_member(scope)
                        if (
                            str(observed.get("list_id") or "") != str(scope["list_id"])
                            or str(observed.get("email_address") or "").casefold()
                            != str(scope["email_address"]).casefold()
                            or str(observed.get("status") or "") != "subscribed"
                        ):
                            raise RuntimeError("subscribe_postcondition_mismatch")
                    except Exception:
                        failed = {
                            "status": "SUBSCRIBE_UNCERTAIN", "subscribed": False,
                            "retry_allowed": False,
                        }
                        self.approval_store.finish_claimed_execution(
                            request_id, action=action, success=False, result=failed,
                        )
                        return _error("SUBSCRIBE_UNCERTAIN")
                    result = {
                        "status": "executed", "state": "SUBSCRIBED",
                        "subscribed": True,
                        "list_id": str(observed.get("list_id") or scope["list_id"]),
                        "email_address": str(observed.get("email_address") or scope["email_address"]),
                        "member_status": str(observed.get("status") or ""),
                        "provider_evidence": dict(observed),
                        "retry_allowed": False,
                    }
                    self.approval_store.finish_claimed_execution(
                        request_id, action=action, success=True, result=result,
                    )
                    return _mutation("subscribe_approved_member", result, writes=1, sends=0)

                try:
                    observed = self.client.send_campaign(str(scope["campaign_id"]))
                    if not observed.get("sent") or campaign_fingerprint(observed) != campaign_fingerprint(before):
                        raise RuntimeError("send_postcondition_mismatch")
                except Exception:
                    failed = {"status": "SEND_UNCERTAIN", "sent": False, "retry_allowed": False}
                    self.approval_store.finish_claimed_execution(
                        request_id, action=action, success=False, result=failed,
                    )
                    return _error("SEND_UNCERTAIN")
                result = {"status": "executed", "state": "SENT", "sent": True,
                          **observed, "retry_allowed": False}
                self.approval_store.finish_claimed_execution(
                    request_id, action=action, success=True, result=result,
                )
                return _mutation("send_approved_campaign", result, writes=1, sends=1)

            if name == "mailchimp_ping":
                payload = self.client.ping()
                return _read(
                    "ping",
                    payload,
                )

            count = int(
                arguments.get("count") or 20
            )
            offset = int(
                arguments.get("offset") or 0
            )

            if name == "mailchimp_list_audiences":
                payload = self.client.list_audiences(
                    count=count,
                    offset=offset,
                )
                return _read(
                    "list_audiences",
                    payload,
                )

            if name == "mailchimp_list_campaigns":
                payload = self.client.list_campaigns(
                    count=count,
                    offset=offset,
                )
                return _read(
                    "list_campaigns",
                    payload,
                )

            if name == "mailchimp_get_campaign_content":
                return _read(
                    "get_campaign_content",
                    self.client.get_campaign_content(str(arguments["campaign_id"])),
                )

            list_id = str(arguments.get("list_id") or "")

            if name == "mailchimp_list_members":
                payload = self.client.list_members(
                    list_id,
                    count=count,
                    offset=offset,
                    status=arguments.get("status"),
                )
                return _read("list_members", payload)

            if name == "mailchimp_list_segments":
                payload = self.client.list_segments(
                    list_id,
                    count=count,
                    offset=offset,
                )
                return _read("list_segments", payload)

            if name == "mailchimp_list_tags":
                payload = self.client.list_tags(
                    list_id,
                    name=arguments.get("name"),
                )
                return _read("list_tags", payload)

            if name == "mailchimp_list_member_tags":
                payload = self.client.list_member_tags(
                    list_id,
                    str(arguments.get("subscriber_hash") or ""),
                )
                return _read("list_member_tags", payload)

            return _error("POLICY_DENIED")

        except MailchimpAPIError as exc:
            return _error(
                exc.status,
                http_status=exc.http_status,
                detail=exc.detail,
            )

        except Exception:
            return _error(
                "SOURCE_UNAVAILABLE"
            )


def _validate(
    arguments: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> str:
    if not isinstance(arguments, Mapping):
        return "POLICY_DENIED"

    properties = schema["properties"]

    if set(arguments) - set(properties):
        return "POLICY_DENIED"

    if set(schema.get("required") or ()) - set(arguments):
        return "POLICY_DENIED"

    for name, value in arguments.items():
        spec = properties[name]

        if spec.get("type") == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                return "POLICY_DENIED"

            minimum = int(
                spec.get("minimum", value)
            )
            maximum = int(
                spec.get("maximum", value)
            )

            if not minimum <= value <= maximum:
                return "POLICY_DENIED"

        if spec.get("type") == "string":
            if not isinstance(value, str):
                return "POLICY_DENIED"
            if len(value) < int(spec.get("minLength", 0)):
                return "POLICY_DENIED"
            if len(value) > int(spec.get("maxLength", len(value))):
                return "POLICY_DENIED"
            if spec.get("enum") and value not in spec["enum"]:
                return "POLICY_DENIED"
            if spec.get("pattern") and re.fullmatch(str(spec["pattern"]), value) is None:
                return "POLICY_DENIED"

    return ""


def _read(
    operation: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "ok": True,
        "operation": operation,
        **dict(payload),
        "side_effects": 0,
        "writes": 0,
        "sends": 0,
    }


def _mutation(operation: str, payload: Mapping[str, Any], *, writes: int, sends: int) -> dict[str, Any]:
    return {
        "ok": True, "operation": operation, **dict(payload),
        "side_effects": writes, "writes": writes, "sends": sends,
    }


def _error(
    code: str,
    *,
    http_status: int | None = None,
    detail: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "status": code,
        "side_effects": 0,
        "writes": 0,
        "sends": 0,
    }

    if http_status is not None:
        payload["http_status"] = http_status

    if detail:
        payload["detail"] = detail

    return {
        "content": [{
            "type": "text",
            "text": code,
        }],
        "structuredContent": payload,
        "isError": True,
    }


def _response(
    request: Mapping[str, Any],
    server: MailchimpMCPServer,
) -> dict[str, Any] | None:
    method = request.get("method")

    if method == "notifications/initialized":
        return None

    request_id = request.get("id")

    if method == "initialize":
        result = {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "ralf-mailchimp",
                "version": "1",
            },
        }

    elif method == "tools/list":
        result = {
            "tools": server.list_tools(),
        }

    elif method == "tools/call":
        params = request.get("params")

        if not isinstance(params, Mapping):
            result = _error(
                "POLICY_DENIED"
            )
        else:
            arguments = params.get(
                "arguments",
                {},
            )

            if not isinstance(arguments, Mapping):
                result = _error(
                    "POLICY_DENIED"
                )
            else:
                result = server.call(
                    str(
                        params.get("name")
                        or ""
                    ),
                    arguments,
                )

    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": -32601,
                "message": "method_not_found",
            },
        }

    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": result,
    }


def _client_from_environment() -> MailchimpClient | None:
    try:
        return MailchimpClient.from_environment()
    except (ValueError, TypeError):
        return None


def main() -> int:
    approval_store = None
    try:
        approval_store = DomainApprovalStore()
    except (OSError, ValueError):
        approval_store = None
    server = MailchimpMCPServer(
        _client_from_environment(), approval_store=approval_store,
    )

    for line in sys.stdin:
        try:
            request = json.loads(line)

            if not isinstance(request, Mapping):
                raise ValueError(
                    "request_not_object"
                )

            response = _response(
                request,
                server,
            )

        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {
                    "code": -32603,
                    "message": "internal_error",
                },
            }

        if response is not None:
            sys.stdout.write(
                json.dumps(
                    response,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
            sys.stdout.flush()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
