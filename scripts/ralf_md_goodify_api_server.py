#!/usr/bin/env python3
from __future__ import annotations

"""Narrow VPN-only HTTP bridge for the Tiremm MD/Goodify Android app."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.md_goodify_transactional import MdGoodifyFlow

MAX_BODY = 8192


class ApiApplication:
    def __init__(self, flow: Any | None = None) -> None:
        self.flow = flow or MdGoodifyFlow()

    def health(self) -> dict[str, Any]:
        return {"ok": True, "service": "md-goodify", "recipient": "TIREMM INNANZ APS"}

    def process(self, value: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if set(value) != {"qr_code"}:
            return 400, {"ok": False, "status": "INVALID_REQUEST"}
        qr = value.get("qr_code")
        if not isinstance(qr, str) or not qr.strip() or len(qr) > 4096:
            return 400, {"ok": False, "status": "INVALID_QR"}
        result = self.flow.process_qr(qr)
        status = str(result.get("status") or "")
        if status == "PROCESSING":
            code = 202
        elif result.get("ok") is True:
            code = 200
        elif status in {"MD_REJECTED", "INVALID_QR"}:
            code = 422
        elif status.startswith("AMBIGUOUS"):
            code = 409
        else:
            code = 502
        return code, dict(result)


class Handler(BaseHTTPRequestHandler):
    server_version = "TiremmMDGoodify/1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    @property
    def app(self) -> ApiApplication:
        return self.server.app  # type: ignore[attr-defined]

    def _json(self, status: int, value: Mapping[str, Any]) -> None:
        raw = json.dumps(dict(value), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/health":
            self._json(404, {"ok": False, "status": "NOT_FOUND"})
            return
        self._json(200, self.app.health())

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/process-qr":
            self._json(404, {"ok": False, "status": "NOT_FOUND"})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() != "application/json":
            self._json(415, {"ok": False, "status": "JSON_REQUIRED"})
            return
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._json(413, {"ok": False, "status": "INVALID_BODY_SIZE"})
            return
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json(400, {"ok": False, "status": "INVALID_JSON"})
            return
        if not isinstance(value, Mapping):
            self._json(400, {"ok": False, "status": "INVALID_REQUEST"})
            return
        try:
            code, result = self.app.process(value)
        except Exception:
            self._json(502, {"ok": False, "status": "BACKEND_ERROR"})
            return
        self._json(code, result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default=os.getenv("RALFLOOP_MD_GOODIFY_API_BIND", "10.252.14.7"))
    parser.add_argument("--port", type=int, default=int(os.getenv("RALFLOOP_MD_GOODIFY_API_PORT", "19234")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.app = ApiApplication()  # type: ignore[attr-defined]
    server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
