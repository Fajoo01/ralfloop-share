from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import urlopen

from .pec_runts import PecAttachment, PecMessage
from .platform import SourceRef


class PecBrowserError(RuntimeError):
    @property
    def status(self) -> str:
        code = str(self)
        if code == "pec_auth_required":
            return "AUTH_REQUIRED"
        if code == "pec_session_expired":
            return "SESSION_EXPIRED"
        if any(part in code for part in ("incomplete", "repeated", "duplicate", "max_pages", "total_changed")):
            return "INCOMPLETE_SOURCE"
        if any(part in code for part in ("malformed", "shape_invalid", "_invalid")):
            return "MALFORMED_RESPONSE"
        return "SOURCE_UNAVAILABLE"


class PecPageTransport(Protocol):
    def first_page(self) -> dict[str, Any]: ...
    def next_page(self) -> dict[str, Any] | None: ...


class PecAuthenticatedCdpTransport:
    """Bounded read navigation over an already-authenticated Aruba PEC tab."""

    HOST = "webmail.pec.it"
    PAGE_PREFIX = "/new/messages/"
    READ_PATH = "/newuismart/cgi-bin/ajaxmail"
    NEXT_SCRIPT = "document.querySelector('webmail-message-paginator aru-button.next:not([disabled])')?.shadowRoot?.querySelector('button:not([disabled])')?.click()"

    def __init__(self, endpoint: str = "http://127.0.0.1:9236", *, timeout_s: float = 8.0) -> None:
        parsed = urlparse(endpoint.rstrip("/"))
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("pec_cdp_endpoint_must_be_loopback")
        self.endpoint, self.timeout_s = endpoint.rstrip("/"), timeout_s
        self._client = None
        self._events = deque()

    def first_page(self) -> dict[str, Any]:
        self.close()
        page = self._page()
        try:
            import websocket
            self._client = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=1)
        except Exception as exc:
            raise PecBrowserError("pec_cdp_unavailable") from exc
        self._call(1, "Network.enable", {"maxTotalBufferSize": 20_000_000, "maxResourceBufferSize": 10_000_000})
        self._call(2, "Page.navigate", {"url": "https://webmail.pec.it/new/messages/INBOX?mail_pnum=1"})
        return self._wait_page()

    def next_page(self) -> dict[str, Any] | None:
        if self._client is None:
            raise PecBrowserError("pec_cdp_not_started")
        result = self._call(3, "Runtime.evaluate", {"expression": self.NEXT_SCRIPT, "returnByValue": True})
        if result.get("exceptionDetails"):
            raise PecBrowserError("pec_paginator_failed")
        try:
            return self._wait_page()
        except PecBrowserError as exc:
            if str(exc) == "pec_page_timeout":
                return None
            raise

    def close(self) -> None:
        self._events.clear()
        if self._client is not None:
            self._client.close()
            self._client = None

    def _page(self) -> dict[str, Any]:
        try:
            with urlopen(self.endpoint + "/json/list", timeout=self.timeout_s) as response:
                rows = json.load(response)
        except Exception as exc:
            raise PecBrowserError("pec_cdp_inventory_unavailable") from exc
        matches = [row for row in rows if row.get("type") == "page" and urlparse(str(row.get("url") or "")).hostname == self.HOST and urlparse(str(row.get("url") or "")).path.startswith(self.PAGE_PREFIX)]
        if not matches:
            raise PecBrowserError("pec_auth_required")
        if len(matches) != 1:
            raise PecBrowserError("pec_authenticated_page_unresolved")
        return matches[0]

    def _call(self, identity: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self._client is not None
        self._client.send(json.dumps({"id": identity, "method": method, "params": params}))
        while True:
            row = json.loads(self._client.recv())
            if row.get("id") == identity:
                if row.get("error"):
                    raise PecBrowserError("pec_cdp_call_failed")
                return row.get("result", {})
            if "method" in row:
                self._events.append(row)

    def _wait_page(self) -> dict[str, Any]:
        assert self._client is not None
        deadline = time.monotonic() + self.timeout_s
        requests: set[str] = set()
        while time.monotonic() < deadline:
            try:
                row = self._events.popleft() if self._events else json.loads(self._client.recv())
            except Exception:
                continue
            params = row.get("params", {})
            request_id = str(params.get("requestId") or "")
            if row.get("method") == "Network.responseReceived" and request_id in requests:
                if params.get("response", {}).get("status") == 401:
                    raise PecBrowserError("pec_session_expired")
            if row.get("method") == "Network.requestWillBeSent":
                request = params.get("request", {})
                if (
                    request.get("method") == "POST"
                    and urlparse(str(request.get("url") or "")).path == self.READ_PATH
                ):
                    requests.add(request_id)
            elif row.get("method") == "Network.loadingFinished" and request_id in requests:
                body = self._call(1000, "Network.getResponseBody", {"requestId": request_id}).get("body", "")
                try:
                    value = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise PecBrowserError("pec_response_malformed") from exc
                if isinstance(value, dict) and isinstance(value.get("data"), list) and isinstance(value.get("pageInfo"), dict):
                    return value
        # Recheck the actual browser boundary before classifying a network timeout.
        self._page()
        raise PecBrowserError("pec_page_timeout")


class PecAuthenticatedBrowserAdapter:
    """Complete, deduplicated inbox reads; never opens messages or changes unread state."""

    def __init__(self, transport: PecPageTransport, *, max_pages: int = 200) -> None:
        self.transport, self.max_pages = transport, max_pages
        self._cache: dict[str, PecMessage] = {}

    def list_messages(self, *, limit: int) -> tuple[PecMessage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("pec_limit_invalid")
        return self._enumerate()[:limit]

    def find_by_runts_reference(self, reference: str, *, limit: int) -> tuple[PecMessage, ...]:
        if not re.fullmatch(r"[A-Za-z0-9_.:@/-]{1,240}", reference) or not 1 <= limit <= 100:
            raise ValueError("pec_reference_invalid")
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(reference) + r"(?![A-Za-z0-9])")
        rows = tuple(row for row in self._enumerate() if pattern.search(row.subject + "\n" + row.body)
                     and "runts" in (row.subject + "\n" + row.body).casefold())
        if len(rows) > limit:
            raise PecBrowserError("pec_incomplete_source")
        return rows

    def _enumerate(self) -> tuple[PecMessage, ...]:
        payload = self.transport.first_page()
        rows: list[PecMessage] = []
        seen_ids: set[str] = set()
        seen_pages: set[int] = set()
        expected_total: int | None = None
        for _ in range(self.max_pages):
            info = payload["pageInfo"]
            page = _integer(info.get("page"), "page")
            total = _integer(info.get("itemCount"), "itemCount")
            if expected_total is None:
                expected_total = total
            elif total != expected_total:
                raise PecBrowserError("pec_total_changed_during_pagination")
            if page in seen_pages:
                raise PecBrowserError("pec_repeated_page")
            seen_pages.add(page)
            for raw in payload["data"]:
                message = _message(raw)
                if message.native_id in seen_ids:
                    raise PecBrowserError("pec_duplicate_message_id")
                seen_ids.add(message.native_id)
                self._cache[message.native_id] = message
                rows.append(message)
            if len(rows) >= expected_total:
                break
            payload = self.transport.next_page()
            if payload is None:
                raise PecBrowserError("pec_incomplete_source")
        else:
            raise PecBrowserError("pec_max_pages_exceeded")
        if expected_total is not None and len(rows) != expected_total:
            raise PecBrowserError("pec_incomplete_source")
        return tuple(rows)

    def get_message(self, native_id: str) -> PecMessage:
        if native_id not in self._cache:
            self.list_messages(limit=100)
        try:
            return self._cache[native_id]
        except KeyError as exc:
            raise PecBrowserError("pec_message_not_found") from exc


def _message(raw: Any) -> PecMessage:
    if not isinstance(raw, dict):
        raise PecBrowserError("pec_message_shape_invalid")
    native_id = str(raw.get("objectId") or raw.get("xruid") or "").strip()
    subject = " ".join(str(raw.get("subject") or "(no subject)").split())[:500]
    sender = " ".join(str(raw.get("email") or raw.get("from") or "unknown@invalid").split())[:320]
    received = _timestamp(raw.get("rawdate") or raw.get("date"))
    observed = datetime.now(timezone.utc)
    body = str(raw.get("text") or "")[:100_000]
    attachments = tuple(_attachments(raw.get("attach")))
    raw_hash = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    source = SourceRef(system="pec", native_id=native_id, locator=f"https://webmail.pec.it/new/messages/INBOX?message={native_id}", observed_at=observed.isoformat(), content_hash=raw_hash)
    reference = _runts_reference(subject + "\n" + body)
    return PecMessage.build(native_id=native_id, subject=subject, sender=sender, received_at=received, observed_at=observed, body=body, unread=_boolean(raw.get("isUnread")), certified=_boolean(raw.get("isCertificataMessage")), attachments=attachments, runts_reference=reference, source=source)


def _attachments(value: Any):
    rows = value if isinstance(value, list) else []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("filename") or raw.get("name") or f"attachment-{index + 1}")[:500]
        identity = str(raw.get("id") or raw.get("objectId") or f"attachment-{index + 1}")[:240]
        yield PecAttachment(attachment_id=identity, filename=name, content_type=(str(raw.get("contentType"))[:160] if raw.get("contentType") else None), size=_optional_integer(raw.get("size")))


def _runts_reference(text: str) -> str | None:
    patterns = (r"(?:idComunicazione|communicationId|messageId)[=/ :]([A-Za-z0-9_.:-]{3,240})", r"(?:RUNTS|pratica)\s*(?:n\.?|id|#)\s*([A-Za-z0-9_.:/-]{3,240})")
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        number = float(value) / (1000 if float(value) > 10_000_000_000 else 1)
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value or "").strip()
    if text:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _integer(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise PecBrowserError(f"pec_{field}_invalid") from exc
    if result < 0:
        raise PecBrowserError(f"pec_{field}_invalid")
    return result


def _optional_integer(value: Any) -> int | None:
    try:
        return max(0, int(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if str(value).casefold() in {"1", "true", "yes"}:
        return True
    if str(value).casefold() in {"0", "false", "no"}:
        return False
    return None


__all__ = ["PecAuthenticatedBrowserAdapter", "PecAuthenticatedCdpTransport", "PecBrowserError", "PecPageTransport"]
