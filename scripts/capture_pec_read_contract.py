#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from urllib.parse import urlparse
from urllib.request import urlopen

import websocket


HOST = "webmail.pec.it"
PATH_PREFIX = "/new/messages/"
SCRIPT = r"""
(() => ({
  location: {origin: location.origin, path: location.pathname},
  resources: performance.getEntriesByType('resource').map(row => {
    const url = new URL(row.name);
    return {origin: url.origin, path: url.pathname, initiator: row.initiatorType};
  }),
  storage_keys: {
    local: Object.keys(localStorage),
    session: Object.keys(sessionStorage),
  },
  forms: [...document.forms].map(form => ({
    method: (form.method || 'get').toUpperCase(),
    action_origin: new URL(form.action || location.href).origin,
    action_path: new URL(form.action || location.href).pathname,
  })),
  inputs: [...document.querySelectorAll('input,select,textarea')].map(row => ({
    tag: row.tagName.toLowerCase(), type: row.type || null,
    name: row.name || null, autocomplete: row.autocomplete || null,
  })),
}))()
""".strip()


def capture(endpoint: str) -> dict:
    with urlopen(endpoint.rstrip("/") + "/json/list", timeout=5) as response:
        pages = json.load(response)
    matches = []
    for page in pages:
        parsed = urlparse(str(page.get("url") or ""))
        if page.get("type") == "page" and parsed.hostname == HOST and parsed.path.startswith(PATH_PREFIX):
            matches.append(page)
    if len(matches) != 1:
        raise RuntimeError("pec_authenticated_page_unresolved")
    client = websocket.create_connection(matches[0]["webSocketDebuggerUrl"], timeout=5)
    try:
        client.send(json.dumps({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": SCRIPT, "returnByValue": True, "awaitPromise": False,
        }}))
        while True:
            message = json.loads(client.recv())
            if message.get("id") == 1:
                if message.get("error") or message.get("result", {}).get("exceptionDetails"):
                    raise RuntimeError("pec_contract_capture_failed")
                value = message["result"]["result"].get("value")
                break
    finally:
        client.close()
    if not isinstance(value, dict):
        raise RuntimeError("pec_contract_capture_malformed")
    resources = {
        (str(row.get("origin") or ""), str(row.get("path") or ""), str(row.get("initiator") or ""))
        for row in value.get("resources", ()) if isinstance(row, dict)
    }
    return {
        "schema_version": 1, "writes": 0, "content_captured": False,
        "location": value.get("location"),
        "resources": [dict(origin=o, path=p, initiator=i) for o, p, i in sorted(resources)],
        "storage_keys": value.get("storage_keys"),
        "forms": value.get("forms"), "inputs": value.get("inputs"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="http://127.0.0.1:9236")
    print(json.dumps(capture(parser.parse_args().endpoint), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
