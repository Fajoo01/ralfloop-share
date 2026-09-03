#!/usr/bin/env python3
from __future__ import annotations

"""Capture sanitized ARCI DataTables contracts from an authenticated browser.

Read-only: enables CDP Network observation, reloads one existing page, records only
the two allowlisted DataTables responses, and emits no credential/header values.
"""

import argparse
import hashlib
import json
import re
import time
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import websocket


HOST = "webapp.tessera-arci.it"
PAGE_PATH = "/club/list-users"
ALLOWED_PAGE_PATHS = {PAGE_PATH, "/club/cards"}
ALLOWED_PATHS = {
    "/api/backoffice/cards/datatables",
    "/api/backoffice/list_users/datatables",
}
PII_KEYS = {
    "name", "first_name", "firstname", "surname", "last_name", "lastname",
    "email", "phone", "telephone", "mobile", "tax_number", "tax_code",
    "fiscal_code", "codice_fiscale", "address", "street", "city", "zip",
    "postal_code", "birthplace", "birth_place", "birthdate", "birth_date",
    "full_name", "user_name", "nome", "cognome", "cellulare", "telefono",
    "telefonoufficio", "codicefiscale", "comunenascita", "datanascita",
    "localita", "numero_civico", "provincia", "via", "nota", "documento",
    "ragione_sociale", "azienda", "fax", "cap",
}
SECRET_HEADERS = {"authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token"}
ID_KEY = re.compile(r"(^id$|_id$|^uuid$|_uuid$)", re.I)


class CaptureError(RuntimeError):
    pass


class Cdp:
    def __init__(self, url: str) -> None:
        self.ws = websocket.create_connection(url, timeout=1)
        self.counter = 0
        self.events: list[dict[str, Any]] = []

    def close(self) -> None:
        self.ws.close()

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.counter += 1
        call_id = self.counter
        self.ws.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == call_id:
                if "error" in message:
                    raise CaptureError(f"cdp_{method}_failed")
                return message.get("result", {})
            self.events.append(message)

    def receive(self, deadline: float) -> dict[str, Any] | None:
        if self.events:
            return self.events.pop(0)
        while time.monotonic() < deadline:
            try:
                return json.loads(self.ws.recv())
            except (TimeoutError, websocket.WebSocketTimeoutException):
                continue
        return None


def _page(endpoint: str) -> dict[str, Any]:
    with urlopen(endpoint.rstrip("/") + "/json/list", timeout=5) as response:
        pages = json.load(response)
    matches = []
    for page in pages:
        parsed = urlparse(str(page.get("url") or ""))
        if page.get("type") == "page" and parsed.hostname == HOST and parsed.path in ALLOWED_PAGE_PATHS:
            matches.append(page)
    if len(matches) != 1:
        raise CaptureError("authenticated_arci_page_unresolved")
    return matches[0]


def _request_payload(raw: str | None, content_type: str) -> Any:
    if not raw:
        return None
    if "json" in content_type:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CaptureError("request_json_malformed") from exc
    if "x-www-form-urlencoded" in content_type:
        return {key: values[0] if len(values) == 1 else values for key, values in parse_qs(raw).items()}
    raise CaptureError("request_content_type_unsupported")


def _synthetic(value: Any, prefix: str, maps: dict[str, dict[str, str]]) -> str:
    raw = str(value)
    bucket = maps[prefix]
    if raw not in bucket:
        bucket[raw] = f"{prefix}-synthetic-{len(bucket) + 1:03d}"
    return bucket[raw]


def _sanitize(value: Any, maps: dict[str, dict[str, str]], key: str = "", *, row_data: bool = False) -> Any:
    folded = key.casefold()
    if folded in PII_KEYS:
        return None if value is None else f"synthetic-{folded.replace('_', '-')}"
    if ID_KEY.search(key) and value is not None:
        prefix = key.casefold().removesuffix("_id").removesuffix("_uuid") or "id"
        return _synthetic(value, prefix, maps)
    if isinstance(value, dict):
        return {str(k): _sanitize(v, maps, str(k), row_data=row_data or key == "data") for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item, maps, key, row_data=row_data or key == "data") for item in value]
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            parsed = urlparse(value)
            return parsed.scheme + "://" + parsed.netloc + "/synthetic-path"
        if row_data and "status" not in folded:
            return f"synthetic-{folded or 'value'}"
        if "@" in value:
            return "synthetic@example.invalid"
        if re.fullmatch(r"[0-9A-Fa-f-]{20,}", value):
            return _synthetic(value, "native", maps)
    return value


def _fixture_response(decoded: Any) -> Any:
    if not isinstance(decoded, dict):
        return decoded
    result = dict(decoded)
    data = result.get("data")
    if isinstance(data, list):
        result["captured_row_count"] = len(data)
        result["data"] = data[:3]
    conf = result.get("conf")
    if isinstance(conf, dict):
        result["conf"] = {"present": True, "keys": sorted(conf)}
    return result


def capture(
    endpoint: str, timeout: float, expected_paths: set[str] | None = None,
    page_path: str = PAGE_PATH,
) -> dict[str, Any]:
    expected_paths = expected_paths or ALLOWED_PATHS
    if not expected_paths or not expected_paths <= ALLOWED_PATHS:
        raise CaptureError("datatable_path_not_allowlisted")
    if page_path not in ALLOWED_PAGE_PATHS:
        raise CaptureError("frontend_path_not_allowlisted")
    page = _page(endpoint)
    client = Cdp(str(page["webSocketDebuggerUrl"]))
    requests: dict[str, dict[str, Any]] = {}
    finished: set[str] = set()
    try:
        client.call("Network.enable", {"maxTotalBufferSize": 20_000_000, "maxResourceBufferSize": 5_000_000})
        client.call("Network.setCacheDisabled", {"cacheDisabled": True})
        client.call("Network.setBypassServiceWorker", {"bypass": True})
        client.call("Page.navigate", {"url": f"https://{HOST}{page_path}"})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            message = client.receive(deadline)
            if message is None:
                break
            method = message.get("method")
            params = message.get("params", {})
            request_id = str(params.get("requestId") or "")
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                parsed = urlparse(str(request.get("url") or ""))
                if parsed.path in expected_paths and request.get("method") == "POST":
                    headers = {str(k).casefold(): str(v) for k, v in request.get("headers", {}).items()}
                    content_type = headers.get("content-type", "")
                    requests[request_id] = {
                        "path": parsed.path,
                        "method": request.get("method"),
                        "headers": {
                            "content_type": content_type,
                            "accept": headers.get("accept"),
                            "credential_headers_present": sorted(set(headers) & SECRET_HEADERS),
                        },
                        "payload": _request_payload(request.get("postData"), content_type),
                    }
            elif method == "Network.responseReceived" and request_id in requests:
                response = params.get("response", {})
                requests[request_id]["response_meta"] = {
                    "status": response.get("status"),
                    "mime_type": response.get("mimeType"),
                }
            elif method == "Network.loadingFinished" and request_id in requests:
                requests[request_id]["response_body"] = client.call(
                    "Network.getResponseBody", {"requestId": request_id}
                ).get("body")
                finished.add(request_id)
            if len({requests[row]["path"] for row in finished}) == len(expected_paths):
                break

        output = []
        maps: dict[str, dict[str, str]] = defaultdict(dict)
        for request_id in sorted(finished, key=lambda row: requests[row]["path"]):
            row = requests[request_id]
            body = row.pop("response_body", None)
            try:
                decoded = json.loads(body)
            except (TypeError, json.JSONDecodeError) as exc:
                raise CaptureError("response_json_malformed") from exc
            sanitized = _sanitize(_fixture_response(decoded), maps)
            canonical = json.dumps(sanitized, sort_keys=True, separators=(",", ":"))
            output.append({
                **row,
                "payload": _sanitize(row.get("payload"), maps),
                "response": sanitized,
                "sanitized_content_hash": hashlib.sha256(canonical.encode()).hexdigest(),
                "test_class": "REAL_CONTRACT",
            })
        paths = {row["path"] for row in output}
        if paths != expected_paths:
            raise CaptureError("datatable_contract_incomplete")
        return {"schema_version": 1, "writes": 0, "captures": output}
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:9236")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--path", action="append", choices=sorted(ALLOWED_PATHS))
    parser.add_argument("--page-path", choices=sorted(ALLOWED_PAGE_PATHS), default=PAGE_PATH)
    args = parser.parse_args()
    print(json.dumps(capture(
        args.endpoint, args.timeout, set(args.path or ALLOWED_PATHS), args.page_path,
    ), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
