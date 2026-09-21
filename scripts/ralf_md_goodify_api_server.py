#!/usr/bin/env python3
from __future__ import annotations

"""Narrow VPN-only HTTP bridge for the Tiremm MD/Goodify Android app."""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import sqlite3
import sys
from decimal import Decimal, InvalidOperation
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
from ralfloop_agent.unified_assistant.md_goodify_readonly import MdGoodifyReadOnlyClient
from ralfloop_agent.unified_assistant.md_goodify_transactional import DEFAULT_STATE_DB, MdGoodifyFlow

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


def _eur(value: Any) -> Decimal | None:
    text = str(value or "").strip().replace("€", "").replace(" ", "")
    if not text:
        return None
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def _completed_tiremm_count(db_path: Path = DEFAULT_STATE_DB) -> int:
    try:
        with sqlite3.connect(db_path) as db:
            row = db.execute(
                "SELECT COUNT(*) FROM qr_flow WHERE phase IN ('COMPLETE','COMPLETE_WIN_UNKNOWN')"
            ).fetchone()
        return int(row[0] if row else 0)
    except (OSError, sqlite3.Error, TypeError, ValueError):
        return 0


class ApiApplication:
    def __init__(self, flow: Any | None = None, authenticator: Any | None = None,
                 history_client: Any | None = None) -> None:
        self.flow = flow or MdGoodifyFlow()
        self.authenticator = authenticator or MdGoodifyAuthenticator()
        self.history = history_client or MdGoodifyReadOnlyClient(timeout=20.0)

    def health(self) -> dict[str, Any]:
        enrolled = token_pair_present()
        return {
            "ok": True,
            "service": "md-goodify",
            "recipient": "TIREMM INNANZ APS",
            "enrolled": enrolled,
        }

    def stats(self) -> tuple[int, dict[str, Any]]:
        try:
            history = self.history.get_donations()
            rows = history.get("donations") if isinstance(history, Mapping) else None
            if not isinstance(rows, list):
                raise ValueError("history_invalid")
            amounts = [amount for row in rows if isinstance(row, Mapping)
                       and (amount := _eur(row.get("Goodify_donatedAmount"))) is not None]
            unique = {amount for amount in amounts if amount > 0}
            unit = next(iter(unique)) if len(unique) == 1 else None
            total = unit * len(rows) if unit is not None else sum(amounts, Decimal("0"))
            tiremm_count = _completed_tiremm_count()
            tiremm_total = unit * tiremm_count if unit is not None else None
            return 200, {
                "ok": True,
                "md_donations_count": len(rows),
                "md_total_eur": format(total, ".2f"),
                "unit_donation_eur": format(unit, ".2f") if unit is not None else None,
                "tiremm_completed_count": tiremm_count,
                "tiremm_total_eur": format(tiremm_total, ".2f") if tiremm_total is not None else None,
            }
        except Exception:
            return 502, {"ok": False, "status": "STATS_UNAVAILABLE"}

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
        if self.path == "/health":
            self._json(200, self.app.health())
            return
        if self.path == "/v1/stats":
            code, result = self.app.stats()
            self._json(code, result)
            return
        self._json(404, {"ok": False, "status": "NOT_FOUND"})

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
