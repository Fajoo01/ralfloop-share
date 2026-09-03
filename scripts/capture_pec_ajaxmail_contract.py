#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import time
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

import websocket


HOST = "webmail.pec.it"
PAGE_PREFIX = "/new/messages/"
READ_PATH = "/newuismart/cgi-bin/ajaxmail"
SAFE_VALUE_KEYS = {"action", "command", "cmd", "operation", "request", "service", "task", "type"}


class Cdp:
    def __init__(self, url: str) -> None:
        self.ws = websocket.create_connection(url, timeout=1)
        self.counter = 0

    def close(self): self.ws.close()

    def call(self, method: str, params: dict | None = None):
        self.counter += 1
        identity = self.counter
        self.ws.send(json.dumps({"id": identity, "method": method, "params": params or {}}))
        while True:
            row = json.loads(self.ws.recv())
            if row.get("id") == identity:
                if row.get("error"):
                    raise RuntimeError("pec_cdp_call_failed")
                return row.get("result", {})


def _page(endpoint: str):
    with urlopen(endpoint.rstrip("/") + "/json/list", timeout=5) as response:
        rows = json.load(response)
    matches = [row for row in rows if row.get("type") == "page" and urlparse(str(row.get("url") or "")).hostname == HOST and urlparse(str(row.get("url") or "")).path.startswith(PAGE_PREFIX)]
    if len(matches) != 1:
        raise RuntimeError("pec_authenticated_page_unresolved")
    return matches[0]


def _payload_schema(raw: str, content_type: str):
    if not raw:
        return {}
    if "json" in content_type:
        try: values = json.loads(raw)
        except json.JSONDecodeError: return {"malformed": True}
    else:
        values = {key: rows[-1] for key, rows in parse_qs(raw, keep_blank_values=True).items()}
    if not isinstance(values, dict):
        return {"type": type(values).__name__}
    output = {}
    for key, value in values.items():
        row = {"type": type(value).__name__}
        folded = str(key).casefold()
        safe_contract_key = folded in SAFE_VALUE_KEYS or folded.startswith("act_") or folded == "tpl"
        if safe_contract_key and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", str(value)):
            row["safe_value"] = value
        output[str(key)] = row
    return output


def _body_schema(body: str, mime: str):
    if len(body) > 10_000_000:
        return {"type": "oversized", "bytes": len(body)}
    if "json" in mime:
        try: return _shape(json.loads(body))
        except json.JSONDecodeError: return {"type": "malformed_json"}
    tags = sorted(set(re.findall(r"<([A-Za-z][A-Za-z0-9:_-]*)\b", body)))
    return {"type": "xml_or_text", "bytes": len(body), "tags": tags[:200]}


def _shape(value, depth=0):
    if depth >= 5: return {"type": type(value).__name__}
    if isinstance(value, dict): return {"type": "object", "fields": {str(key): _shape(child, depth + 1) for key, child in value.items()}}
    if isinstance(value, list): return {"type": "array", "count": len(value), "item": _shape(value[0], depth + 1) if value else None}
    return {"type": type(value).__name__, "nullable": value is None}


def capture(endpoint: str, timeout: float):
    page = _page(endpoint)
    client = Cdp(page["webSocketDebuggerUrl"])
    requests, output = {}, []
    try:
        client.call("Network.enable", {"maxTotalBufferSize": 20_000_000, "maxResourceBufferSize": 10_000_000})
        client.call("Page.reload", {"ignoreCache": True})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try: row = json.loads(client.ws.recv())
            except websocket.WebSocketTimeoutException: continue
            method, params = row.get("method"), row.get("params", {})
            request_id = str(params.get("requestId") or "")
            if method == "Network.requestWillBeSent":
                request = params.get("request", {})
                if urlparse(str(request.get("url") or "")).path == READ_PATH:
                    headers = {str(k).casefold(): str(v) for k, v in request.get("headers", {}).items()}
                    content_type = headers.get("content-type", "")
                    requests[request_id] = {
                        "method": request.get("method"), "path": READ_PATH,
                        "content_type": content_type,
                        "credential_headers_present": sorted(set(headers) & {"authorization", "cookie", "x-api-key", "x-auth-token"}),
                        "payload_schema": _payload_schema(str(request.get("postData") or ""), content_type),
                    }
            elif method == "Network.responseReceived" and request_id in requests:
                response = params.get("response", {})
                requests[request_id].update(status=response.get("status"), mime=response.get("mimeType"))
            elif method == "Network.loadingFinished" and request_id in requests:
                body = client.call("Network.getResponseBody", {"requestId": request_id}).get("body", "")
                meta = requests.pop(request_id)
                output.append({**meta, "response_schema": _body_schema(body, str(meta.get("mime") or ""))})
        return {"schema_version": 1, "writes": 0, "content_captured": False, "captures": output}
    finally:
        client.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:9236")
    parser.add_argument("--timeout", type=float, default=8)
    args = parser.parse_args()
    print(json.dumps(capture(args.endpoint, args.timeout), indent=2, sort_keys=True))


if __name__ == "__main__": main()
