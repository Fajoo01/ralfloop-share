#!/usr/bin/env python3
from __future__ import annotations

"""Read-only Mailchimp Marketing API MCP server for Ralfloop."""

import base64
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

        request = Request(
            url,
            headers={
                "Authorization": f"Basic {credentials}",
                "Accept": "application/json",
                "User-Agent": "ralfloop-mailchimp-mcp/1",
            },
            method="GET",
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


class MailchimpMCPServer:
    def __init__(
        self,
        client: MailchimpClient | None,
    ) -> None:
        self.client = client

    def list_tools(self) -> list[dict[str, Any]]:
        descriptions = {
            "mailchimp_ping":
                "Check authenticated Mailchimp Marketing API availability.",
            "mailchimp_list_audiences":
                "List Mailchimp audiences without modifying remote data.",
            "mailchimp_list_campaigns":
                "List Mailchimp campaigns without modifying remote data.",
            "mailchimp_list_members":
                "List minimized Mailchimp audience members without modifying them.",
            "mailchimp_list_segments":
                "List Mailchimp audience segments without modifying them.",
            "mailchimp_list_tags":
                "List or search Mailchimp audience tags without modifying them.",
            "mailchimp_list_member_tags":
                "List tags for one Mailchimp member without modifying them.",
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
    server = MailchimpMCPServer(
        _client_from_environment()
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
