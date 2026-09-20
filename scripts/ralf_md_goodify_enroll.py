#!/usr/bin/env python3
"""Loopback-only one-time enrollment for MD credentials.

The password is sent only to the MD login API, is never logged, and is never
stored. Successful login writes only access/refresh tokens to private files.
"""

from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import secrets
import sys
import threading
from urllib.parse import parse_qs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ralfloop_agent.unified_assistant.md_goodify_auth import (
    MdGoodifyAuthenticator,
    MdLoginRejected,
)

HOST = "127.0.0.1"
PORT = 19197
MAX_FORM_BYTES = 16384


def page(nonce: str, message: str = "", *, success: bool = False) -> bytes:
    notice = f"<p><strong>{escape(message)}</strong></p>" if message else ""
    form = "" if success else f"""
<form method="post" action="/enroll" autocomplete="on">
  <input type="hidden" name="nonce" value="{escape(nonce)}">
  <label>Email MD <input type="email" name="email" autocomplete="username" required></label><br>
  <label>Password MD <input type="password" name="password" autocomplete="current-password" required></label><br>
  <button type="submit">Collega MD a Bot-tazzi</button>
</form>"""
    return f"""<!doctype html><meta charset="utf-8">
<title>Bot-tazzi · collega MD</title>
<style>body{{font:18px sans-serif;max-width:620px;margin:4rem auto;padding:1rem}}input{{margin:.5rem;padding:.4rem}}button{{margin-top:1rem;padding:.7rem}}</style>
<h1>Collega account MD</h1>
<p>Le credenziali vengono usate una sola volta per l'accesso MD. La password non viene salvata.</p>
{notice}{form}""".encode("utf-8")


class EnrollmentServer(HTTPServer):
    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.nonce = secrets.token_urlsafe(32)
        self.authenticator = MdGoodifyAuthenticator()


class Handler(BaseHTTPRequestHandler):
    server: EnrollmentServer

    def log_message(self, _format, *_args):
        return

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/":
            self._send(404, b"not found")
            return
        self._send(200, page(self.server.nonce))

    def do_POST(self):
        if self.path != "/enroll":
            self._send(404, b"not found")
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            size = 0
        if size <= 0 or size > MAX_FORM_BYTES:
            self._send(400, page(self.server.nonce, "Richiesta non valida."))
            return
        fields = parse_qs(self.rfile.read(size).decode("utf-8", "strict"), keep_blank_values=True)
        nonce = (fields.get("nonce") or [""])[0]
        email = (fields.get("email") or [""])[0]
        password = (fields.get("password") or [""])[0]
        if not secrets.compare_digest(nonce, self.server.nonce):
            self._send(403, page(self.server.nonce, "Sessione non valida: ricarica la pagina."))
            return
        try:
            result = self.server.authenticator.login(email, password)
        except MdLoginRejected as exc:
            self._send(401, page(self.server.nonce, str(exc)))
            return
        except Exception:
            self._send(502, page(self.server.nonce, "Accesso MD non riuscito. Nessuna credenziale salvata."))
            return
        finally:
            password = ""
        if not result.get("access_token_saved"):
            self._send(502, page(self.server.nonce, "Token MD non ricevuto."))
            return
        self._send(200, page(self.server.nonce, "Account MD collegato. Puoi chiudere questa scheda.", success=True))
        threading.Thread(target=self.server.shutdown, daemon=True).start()


def main() -> int:
    server = EnrollmentServer((HOST, PORT), Handler)
    print(f"READY http://{HOST}:{PORT}/", flush=True)
    server.serve_forever()
    print("ENROLLED=1", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
