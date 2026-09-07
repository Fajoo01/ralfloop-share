from __future__ import annotations

import hashlib
import base64
import json
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import urlopen

from .pec_runts import RuntsAttachment, RuntsAuthRequired, RuntsMessage, RuntsPractice
from .platform import SourceRef


class RuntsBrowserError(RuntimeError):
    pass


class RuntsPayloadTransport(Protocol):
    def pages(self, kind: str) -> tuple[dict[str, Any], ...]: ...


class RuntsAuthenticatedCdpTransport:
    """Observe a frontend READ, then repeat only its validated GET pagination."""

    HOST = "runts.lavoro.gov.it"
    API_HOSTS = {"api.terzosettore.infocamere.it", "ista.scrivaniapa.infocamere.it"}
    TARGETS = {
        "practices": ("Lista Pratiche", "/api/v1/istanza/lista"),
        "messages": ("Messaggi", "/api/v1/messaggio/"),
    }
    CLICK = """function(label) {
      const node = Array.from(document.querySelectorAll('a,button')).find(
        item => String(item.innerText || '').trim() === label
      );
      if (!node) return false;
      node.click();
      return true;
    }"""

    def __init__(self, endpoint: str = "http://127.0.0.1:9236", *, timeout_s: float = 12.0, max_pages: int = 200) -> None:
        parsed = urlparse(endpoint.rstrip("/"))
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("runts_cdp_endpoint_must_be_loopback")
        self.endpoint, self.timeout_s, self.max_pages = endpoint.rstrip("/"), timeout_s, max_pages

    def pages(self, kind: str) -> tuple[dict[str, Any], ...]:
        if kind not in self.TARGETS:
            raise ValueError("runts_read_kind_denied")
        page = self._page()
        import websocket
        ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=1)
        counter = 0
        events = deque()

        def call(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal counter
            counter += 1
            identity = counter
            ws.send(json.dumps({"id": identity, "method": method, "params": params}))
            while True:
                row = json.loads(ws.recv())
                if row.get("id") == identity:
                    if row.get("error"):
                        raise RuntsBrowserError("runts_cdp_call_failed")
                    return row.get("result", {})
                if "method" in row:
                    events.append(row)

        try:
            call("Network.enable", {"maxTotalBufferSize": 30_000_000, "maxResourceBufferSize": 15_000_000})
            label, path = self.TARGETS[kind]
            call("Runtime.evaluate", {"expression": "location.assign('/frontoffice/home')", "returnByValue": True})
            time.sleep(1.5)
            clicked = call("Runtime.evaluate", {"expression": f"({self.CLICK})({json.dumps(label)})", "awaitPromise": True, "returnByValue": True}).get("result", {}).get("value")
            if clicked is not True:
                raise RuntsAuthRequired("authenticated_runts_navigation_unavailable")
            first, url = self._wait_read(ws, call, path, events)
            total_pages = _integer(first.get("totalPages"), "totalPages")
            if total_pages > self.max_pages:
                raise RuntsBrowserError("runts_max_pages_exceeded")
            output = [first]
            current = _integer(parse_qs(urlparse(url).query).get("page", [0])[-1], "page")
            if current != 0:
                raise RuntsBrowserError("runts_incomplete_source")
            for page_number in range(current + 1, total_pages):
                call("Runtime.evaluate", {"expression": "document.querySelector('li.page-item:not(.disabled) a[aria-label=Next]')?.click()", "returnByValue": True})
                body, next_url = self._wait_read(ws, call, path, events)
                actual = _integer(parse_qs(urlparse(next_url).query).get("page", [-1])[-1], "page")
                if actual != page_number:
                    raise RuntsBrowserError("runts_incomplete_source")
                output.append(body)
            return tuple(output)
        finally:
            ws.close()

    def _page(self) -> dict[str, Any]:
        try:
            with urlopen(self.endpoint + "/json/list", timeout=self.timeout_s) as response:
                rows = json.load(response)
        except Exception as exc:
            raise RuntsBrowserError("runts_cdp_inventory_unavailable") from exc
        matches = [row for row in rows if row.get("type") == "page" and urlparse(str(row.get("url") or "")).hostname == self.HOST and urlparse(str(row.get("url") or "")).path.startswith("/frontoffice/")]
        if len(matches) != 1:
            raise RuntsAuthRequired("authenticated_runts_page_unresolved")
        return matches[0]

    def download_attachment(self, practice_id: str, attachment_id: str) -> bytes:
        """Use the observed read-only viewer; reject ambiguous DOM or response scope."""
        pages = self.pages("messages")
        if any(p.get("_read_context", {}).get("practice_id") != practice_id for p in pages):
            raise RuntsBrowserError("runts_practice_scope_mismatch")
        attachments = [a for p in pages for m in p["listaMessaggi"] for a in m.get("messaggioDocumentos", [])]
        # The observed viewer cannot identify one of several attachments safely yet.
        if len(attachments) != 1 or str(attachments[0].get("idMessaggioDocumento")) != attachment_id:
            raise RuntsBrowserError("runts_attachment_selection_unproven")
        import websocket
        ws = websocket.create_connection(self._page()["webSocketDebuggerUrl"], timeout=1)
        events = deque()
        counter = 0

        def call(method, params):
            nonlocal counter
            counter += 1
            ws.send(json.dumps({"id": counter, "method": method, "params": params}))
            while True:
                row = json.loads(ws.recv())
                if row.get("id") == counter:
                    if row.get("error"):
                        raise RuntsBrowserError("runts_cdp_call_failed")
                    return row.get("result", {})
                events.append(row)

        try:
            call("Network.enable", {"maxResourceBufferSize": 20_000_000, "maxTotalBufferSize": 30_000_000})
            # Chrome may decode an incorrectly labelled PDF response as UTF-8.
            # Observe the frontend's original Blob instead; never re-encode lossy text.
            call("Runtime.evaluate", {"expression": "(() => { if(window.__bottazziPdfRead) throw Error('read_busy'); const original=URL.createObjectURL; const state={original,bytes:null}; window.__bottazziPdfRead=state; URL.createObjectURL=function(blob){ const url=original.call(this,blob); if(blob instanceof Blob && blob.size<=20000000){state.bytes=blob.arrayBuffer().then(b=>{let s='';const a=new Uint8Array(b);for(let i=0;i<a.length;i+=8192)s+=String.fromCharCode(...a.subarray(i,i+8192));return btoa(s)});} return url; }; })()"})
            value = call("Runtime.evaluate", {"expression": "(() => {const a=Array.from(document.querySelectorAll('a')).filter(e=>e.innerText.trim()==='Scarica selezionato'); if(a.length!==1)return false;a[0].click();return true})()", "returnByValue": True})
            if value.get("result", {}).get("value") is not True:
                raise RuntsBrowserError("runts_attachment_selection_unproven")
            pending = set()
            deadline = time.monotonic() + self.timeout_s
            while time.monotonic() < deadline:
                try:
                    row = events.popleft() if events else json.loads(ws.recv())
                except websocket.WebSocketTimeoutException:
                    continue
                p = row.get("params", {})
                request_id = p.get("requestId")
                if row.get("method") == "Network.requestWillBeSent":
                    request = p.get("request", {})
                    url = urlparse(request.get("url", ""))
                    query = parse_qs(url.query)
                    if request.get("method") == "GET" and url.hostname in self.API_HOSTS and url.path == "/api/v1/messaggio/allegato/" + attachment_id and query.get("idIstanza") == [practice_id]:
                        pending.add(request_id)
                elif row.get("method") == "Network.responseReceived" and request_id in pending:
                    status = p.get("response", {}).get("status")
                    if status == 401:
                        raise RuntsAuthRequired("runts_session_expired")
                    if status != 200:
                        raise RuntsBrowserError("runts_attachment_unavailable")
                elif row.get("method") == "Network.loadingFinished" and request_id in pending:
                    result = call("Network.getResponseBody", {"requestId": request_id})
                    if result.get("base64Encoded"):
                        body = base64.b64decode(result["body"], validate=True)
                    else:
                        result = call("Runtime.evaluate", {"expression": "(async()=>{for(let i=0;i<40;i++){if(window.__bottazziPdfRead?.bytes)return await window.__bottazziPdfRead.bytes;await new Promise(r=>setTimeout(r,50));}return null})()", "awaitPromise": True, "returnByValue": True})
                        encoded = result.get("result", {}).get("value")
                        if not isinstance(encoded, str):
                            raise RuntsBrowserError("runts_attachment_binary_unavailable")
                        body = base64.b64decode(encoded, validate=True)
                    if not body.startswith(b"%PDF-") or len(body) > 20_000_000:
                        raise RuntsBrowserError("runts_attachment_shape_invalid")
                    return body
            raise RuntsBrowserError("runts_attachment_timeout")
        finally:
            try:
                call("Runtime.evaluate", {"expression": "if(window.__bottazziPdfRead){URL.createObjectURL=window.__bottazziPdfRead.original;delete window.__bottazziPdfRead}"})
            finally:
                ws.close()

    def _wait_read(self, ws: Any, call: Any, path: str, events: Any) -> tuple[dict[str, Any], str]:
        deadline = time.monotonic() + self.timeout_s
        requests: dict[str, str] = {}
        while time.monotonic() < deadline:
            try:
                row = events.popleft() if events else json.loads(ws.recv())
            except Exception:
                continue
            params = row.get("params", {})
            request_id = str(params.get("requestId") or "")
            if row.get("method") == "Network.requestWillBeSent":
                request = params.get("request", {})
                parsed = urlparse(str(request.get("url") or ""))
                if request.get("method") == "GET" and parsed.hostname in self.API_HOSTS and parsed.path.startswith(path):
                    requests[request_id] = request["url"]
            elif row.get("method") == "Network.responseReceived" and request_id in requests:
                if params.get("response", {}).get("status") == 401:
                    raise RuntsAuthRequired("runts_session_expired")
            elif row.get("method") == "Network.loadingFinished" and request_id in requests:
                body = call("Network.getResponseBody", {"requestId": request_id}).get("body", "")
                try:
                    value = json.loads(body)
                except json.JSONDecodeError as exc:
                    raise RuntsBrowserError("runts_response_malformed") from exc
                if not isinstance(value, dict) or value.get("success") is not True:
                    raise RuntsBrowserError("runts_response_unsuccessful")
                url = requests[request_id]
                parsed = urlparse(url)
                # Provenance excludes user IDs and authentication query parameters.
                value["_read_context"] = {"locator": parsed.scheme + "://" + parsed.netloc + parsed.path}
                if path == self.TARGETS["messages"][1]:
                    identity = parsed.path.removeprefix(path)
                    if not identity.isdecimal():
                        raise RuntsBrowserError("runts_practice_scope_invalid")
                    value["_read_context"]["practice_id"] = identity
                return value, url
        raise RuntsBrowserError("runts_read_timeout")

    @staticmethod
    def _document(call: Any) -> str:
        result = call("Runtime.evaluate", {"expression": "document", "returnByValue": False})
        object_id = result.get("result", {}).get("objectId")
        if not object_id:
            raise RuntsBrowserError("runts_document_unavailable")
        return object_id


class RuntsAuthenticatedBrowserAdapter:
    def __init__(self, transport: RuntsPayloadTransport, *, max_items: int = 5000) -> None:
        self.transport, self.max_items = transport, max_items
        self._messages: dict[str, RuntsMessage] = {}
        self._practices: dict[str, RuntsPractice] = {}

    def list_messages(self, practice_id: str | None = None, *, limit: int = 100) -> tuple[RuntsMessage, ...]:
        if practice_id is None:
            raise RuntsBrowserError("runts_practice_scope_required")
        rows = self._enumerate("messages", "listaMessaggi", practice_id=practice_id)
        messages = tuple(_message(row) for row in rows)
        if practice_id is not None and any(row.practice_id != practice_id for row in messages):
            raise RuntsBrowserError("runts_practice_scope_mismatch")
        if len(messages) > _limit(limit):
            raise RuntsBrowserError("runts_incomplete_source")
        self._messages.update((row.native_id, row) for row in messages)
        return messages[:_limit(limit)]

    def get_message(self, native_id: str) -> RuntsMessage:
        if native_id not in self._messages:
            raise RuntsBrowserError("runts_message_scope_required")
        try:
            return self._messages[native_id]
        except KeyError as exc:
            raise RuntsBrowserError("runts_message_not_found") from exc

    def list_practices(self, *, limit: int) -> tuple[RuntsPractice, ...]:
        rows = self._enumerate("practices", "listaIstanze")
        practices = tuple(_practice(row) for row in rows)
        self._practices.update((row.native_id, row) for row in practices)
        return practices[:_limit(limit)]

    def get_practice(self, native_id: str) -> RuntsPractice:
        if native_id not in self._practices:
            self.list_practices(limit=100)
        try:
            return self._practices[native_id]
        except KeyError as exc:
            raise RuntsBrowserError("runts_practice_not_found") from exc

    def _enumerate(self, kind: str, field: str, *, practice_id: str | None = None) -> tuple[dict[str, Any], ...]:
        pages = self.transport.pages(kind)
        if not pages:
            raise RuntsBrowserError("runts_incomplete_source")
        expected = _integer(pages[0].get("totalElements"), "totalElements")
        total_pages = _integer(pages[0].get("totalPages"), "totalPages")
        if len(pages) != max(1, total_pages) or (total_pages == 0 and expected != 0):
            raise RuntsBrowserError("runts_incomplete_source")
        rows: list[dict[str, Any]] = []
        identities: set[str] = set()
        identity_field = "idMessaggio" if kind == "messages" else "idIstanza"
        for payload in pages:
            if practice_id is not None and payload.get("_read_context", {}).get("practice_id") != practice_id:
                raise RuntsBrowserError("runts_practice_scope_mismatch")
            if _integer(payload.get("totalElements"), "totalElements") != expected or _integer(payload.get("totalPages"), "totalPages") != total_pages:
                raise RuntsBrowserError("runts_pagination_changed")
            values = payload.get(field)
            if not isinstance(values, list):
                raise RuntsBrowserError("runts_response_shape_invalid")
            for row in values:
                if not isinstance(row, dict) or not str(row.get(identity_field) or ""):
                    raise RuntsBrowserError("runts_identity_missing")
                identity = str(row[identity_field])
                if identity in identities:
                    raise RuntsBrowserError("runts_duplicate_identity")
                identities.add(identity)
                rows.append({**row, "_read_context": payload.get("_read_context", {})})
        if len(rows) != expected or len(rows) > self.max_items:
            raise RuntsBrowserError("runts_incomplete_source")
        return tuple(rows)


def _message(raw: dict[str, Any]) -> RuntsMessage:
    native_id = str(raw["idMessaggio"])
    observed = datetime.now(timezone.utc)
    attachments = tuple(_attachment(row, observed) for row in (raw.get("messaggioDocumentos") or ()) if isinstance(row, dict))
    source = _source("message", native_id, raw, observed)
    nested = _nested_id(raw.get("istanza"), "idIstanza")
    scoped = raw.get("_read_context", {}).get("practice_id")
    if nested and scoped and nested != scoped:
        raise RuntsBrowserError("runts_practice_scope_mismatch")
    return RuntsMessage.build(native_id=native_id, practice_id=nested or scoped, subject=str(raw.get("oggetto") or raw.get("nota") or "RUNTS message")[:500], body=str(raw.get("corpo") or "")[:100_000], published_at=_timestamp(raw.get("dtOraIns")) if raw.get("dtOraIns") is not None else None, observed_at=observed, action_required=False, attachments=attachments, source=source)


def _practice(raw: dict[str, Any]) -> RuntsPractice:
    native_id = str(raw["idIstanza"])
    observed = datetime.now(timezone.utc)
    state = raw.get("decoStato") if isinstance(raw.get("decoStato"), dict) else {}
    status = str(state.get("codice") or state.get("descrizione") or "UNKNOWN")[:160]
    compliance = raw.get("adempimento") if isinstance(raw.get("adempimento"), dict) else {}
    title = str(compliance.get("descrizione") or raw.get("cistanza") or "RUNTS practice")[:500]
    source = _source("practice", native_id, raw, observed)
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return RuntsPractice(native_id=native_id, status_raw=status, title=title, updated_at=_timestamp(raw.get("dtOraMod")), observed_at=observed, action_required=False, source=source, content_hash=digest)


def _attachment(raw: dict[str, Any], observed: datetime) -> RuntsAttachment:
    native_id = str(raw.get("idMessaggioDocumento") or raw.get("idStorage") or "")
    if not native_id:
        raise RuntsBrowserError("runts_attachment_identity_missing")
    source = _source("attachment", native_id, raw, observed)
    return RuntsAttachment(native_id=native_id, name=str(raw.get("nomeFile") or "RUNTS attachment")[:500], document_type=(str((raw.get("decoTipo") or {}).get("codice"))[:160] if isinstance(raw.get("decoTipo"), dict) and (raw.get("decoTipo") or {}).get("codice") else None), content_hash=None, source=source)


def _source(kind: str, native_id: str, raw: dict[str, Any], observed: datetime) -> SourceRef:
    digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
    return SourceRef(system="runts", native_id=native_id, locator=raw.get("_read_context", {}).get("locator") or "https://runts.lavoro.gov.it/frontoffice/", observed_at=observed.isoformat(), content_hash=digest)


def _nested_id(value: Any, field: str) -> str | None:
    return str(value[field]) if isinstance(value, dict) and value.get(field) is not None else None


def _timestamp(value: Any) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value) / (1000 if float(value) > 10_000_000_000 else 1), tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError as exc:
        raise RuntsBrowserError("runts_timestamp_invalid") from exc


def _integer(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntsBrowserError(f"runts_{field}_invalid") from exc
    if result < 0:
        raise RuntsBrowserError(f"runts_{field}_invalid")
    return result


def _page_url(url: str, page: int) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in RuntsAuthenticatedCdpTransport.API_HOSTS:
        raise RuntsBrowserError("runts_page_url_denied")
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page)]
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(query, doseq=True), ""))


def _limit(value: int) -> int:
    if not 1 <= value <= 100:
        raise ValueError("runts_limit_invalid")
    return value


__all__ = ["RuntsAuthenticatedBrowserAdapter", "RuntsAuthenticatedCdpTransport", "RuntsBrowserError", "RuntsPayloadTransport"]
