"""Read-only live access to MD Goodify donation history.

Only the reverse-engineered ``getdonation`` endpoint is reachable here.
There is deliberately no network implementation for ``purchasedonation``.
"""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import stat
from typing import Any, Callable

from .md_goodify_api import build_get_donation_body, parse_goodify_response

GOODIFY_HOST = "catalogomdapp.dedagroupwiz.it"
GET_DONATION_PATH = "/api/goodify/getdonation"
DEFAULT_TOKEN_FILE = Path("/var/lib/ralfloop/md-goodify/access-token")
MAX_RESPONSE_BYTES = 1024 * 1024

ConnectionFactory = Callable[..., http.client.HTTPSConnection]


def token_file_from_env() -> Path:
    value = os.getenv("RALFLOOP_MD_GOODIFY_TOKEN_FILE", "").strip()
    return Path(value) if value else DEFAULT_TOKEN_FILE


def load_access_token(path: Path | None = None) -> str:
    target = path or token_file_from_env()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except FileNotFoundError as exc:
        raise RuntimeError("md_goodify_token_unavailable") from exc
    except OSError as exc:
        raise RuntimeError("md_goodify_token_file_invalid") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("md_goodify_token_file_invalid")
        if info.st_mode & 0o077:
            raise RuntimeError("md_goodify_token_file_permissions")
        if info.st_uid not in {0, os.geteuid()}:
            raise RuntimeError("md_goodify_token_file_owner")
        raw = os.read(fd, 16385)
    finally:
        os.close(fd)
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("md_goodify_token_invalid") from exc
    if not token or len(raw) > 16384:
        raise RuntimeError("md_goodify_token_invalid")
    return token


class MdGoodifyReadOnlyClient:
    def __init__(
        self,
        *,
        token_path: Path | None = None,
        timeout: float = 12.0,
        connection_factory: ConnectionFactory = http.client.HTTPSConnection,
    ) -> None:
        self.token_path = token_path
        self.timeout = timeout
        self.connection_factory = connection_factory

    def get_donations(self) -> dict[str, Any]:
        token = load_access_token(self.token_path)
        body = json.dumps(
            build_get_donation_body(token),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = self.connection_factory(GOODIFY_HOST, 443, timeout=self.timeout)
        try:
            connection.request(
                "POST",
                GET_DONATION_PATH,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("md_goodify_response_too_large")
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"md_goodify_http_{response.status}")
        parsed = parse_goodify_response(raw)
        return {
            "code": parsed.code,
            "message": parsed.message,
            "donations": list(parsed.donations),
            "network_requests": 1,
            "mutations": 0,
            "submission_performed": False,
            "side_effects": 0,
        }
