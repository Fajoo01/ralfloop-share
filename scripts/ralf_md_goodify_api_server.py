#!/usr/bin/env python3
from __future__ import annotations

"""Narrow VPN-only HTTP bridge for the Tiremm MD/Goodify Android app."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.md_goodify_auth import (
    ACCESS_TOKEN_FILE,
    REFRESH_TOKEN_FILE,
    ROOT,
    MdGoodifyAuthenticator,
    MdLoginRejected,
    _load_private_text,
)
from ralfloop_agent.unified_assistant.md_goodify_transactional import MdGoodifyFlow

MAX_BODY = 16384
APP_TOKEN_FILE = ROOT / "app-token"


def load_app_token() -> str:
    return _load_private_text(APP_TOKEN_FILE, max_bytes=512)


def authorized_header(value: str, expected_token: str) -> bool:
    prefix = "Bearer "
    if not isinstance(value, str) or not value.startswith(prefix):
        return False
    supplied = value[len(prefix):]
    return bool(supplied) and hmac.compare_digest(supplied, expected_token)


def token_pair_present() -> bool:
    try:
        return ACCESS_TOKEN_FILE.exists() and REFRESH_TOKEN_FILE.exists()
    except OSError:
        return False


class ApiApplication:
    def __init__(self, flow: Any | None = None, authenticator: Any | None = None) -> None:
        self.flow = flow or MdGoodifyFlow()
        self.authenticator = authenticator or MdGoodifyAuthenticator()

    def health(self) -> dict[str, Any]:
        enrolled = token_pair_present()
        return {
            "ok": True,
            "service": "md-goodify",
            "recipient": "TIREMM INNANZ APS",
            "enrolled": enrolled,
        }

    def enroll(self, value: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        if set(value) != {"email", "password"}:
            return 400, {"ok": False, "status": "INVALID_REQUEST"}
        email = value.get("email")
        password = value.get("password")
        if not isinstance(email, str) or not isinstance(password, str):
            return 400, {"ok": False, "status": "INVALID_REQUEST"}
        try:
            result = self.authenticator.login(email, password)
        except (MdLoginRejected, ValueError):
            return 401, {"ok": False, "status": "LOGIN_REJECTED"}
        except Exception:
            return 502, {"ok": False, "status": "LOGIN_ERROR"}
        return 200, {
            "ok": bool(result.get("access_token_saved")),
            "status": "ENROLLED" if result.get("access_token_saved") else "LOGIN_ERROR",
        }

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

    @property
    def api_token(self) -> str:
        return self.server.api_token  # type: ignore[attr-defined]

    def _authorized(self) -> bool:
        return authorized_header(self.headers.get("Authorization", ""), self.api_token)

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
        if not self._authorized():
            self._json(401, {"ok": False, "status": "UNAUTHORIZED"})
            return
        if self.path != "/health":
            self._json(404, {"ok": False, "status": "NOT_FOUND"})
            return
        self._json(200, self.app.health())

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._json(401, {"ok": False, "status": "UNAUTHORIZED"})
            return
        if self.path not in {"/v1/process-qr", "/v1/enroll"}:
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
            if self.path == "/v1/enroll":
                code, result = self.app.enroll(value)
            else:
                code, result = self.app.process(value)
        except Exception:
            self._json(502, {"ok": False, "status": "BACKEND_ERROR"})
            return
        self._json(code, result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bind", default=os.getenv("RALFLOOP_MD_GOODIFY_API_BIND", "10.44.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("RALFLOOP_MD_GOODIFY_API_PORT", "19234")))
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Handler)
    server.app = ApiApplication()  # type: ignore[attr-defined]
    server.api_token = load_app_token()  # type: ignore[attr-defined]
    server.serve_forever(poll_interval=0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
