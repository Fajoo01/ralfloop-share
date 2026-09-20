"""One-time MD authentication for the read-only Goodify integration."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Callable, Mapping

AUTH_HOST = "api.platform-backend.mdspa.it"
LOGIN_PATH = "/auth/login"
REFRESH_PATH = "/auth/refresh-token"
BUILD_VERSION = "216"
ROOT = Path("/var/lib/ralfloop/md-goodify")
API_KEY_FILE = ROOT / "api-key"
DEVICE_ID_FILE = ROOT / "device-id"
ACCESS_TOKEN_FILE = ROOT / "access-token"
REFRESH_TOKEN_FILE = ROOT / "refresh-token"
MAX_RESPONSE_BYTES = 1024 * 1024
ConnectionFactory = Callable[..., http.client.HTTPSConnection]


class MdLoginRejected(RuntimeError):
    pass


def _load_private_text(path: Path, *, max_bytes: int = 16384) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError as exc:
        raise RuntimeError(f"md_secret_missing:{path.name}") from exc
    except OSError as exc:
        raise RuntimeError(f"md_secret_invalid:{path.name}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise RuntimeError(f"md_secret_permissions:{path.name}")
        if info.st_uid not in {0, os.geteuid()}:
            raise RuntimeError(f"md_secret_owner:{path.name}")
        raw = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(raw) > max_bytes:
        raise RuntimeError(f"md_secret_too_large:{path.name}")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"md_secret_invalid:{path.name}") from exc
    if not value:
        raise RuntimeError(f"md_secret_empty:{path.name}")
    return value


def _write_private_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, value.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def ensure_device_id(path: Path = DEVICE_ID_FILE) -> str:
    try:
        return _load_private_text(path, max_bytes=128)
    except RuntimeError as exc:
        if not str(exc).startswith("md_secret_missing:"):
            raise
    value = secrets.token_hex(8)
    _write_private_text(path, value)
    return value


def _login_payload(response: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = response.get("payload")
    if isinstance(payload, Mapping):
        return payload
    message = str(response.get("messaggio") or "Accesso MD non riuscito.")
    raise MdLoginRejected(message[:240])


class MdGoodifyAuthenticator:
    def __init__(
        self,
        *,
        api_key_path: Path = API_KEY_FILE,
        device_id_path: Path = DEVICE_ID_FILE,
        access_token_path: Path = ACCESS_TOKEN_FILE,
        refresh_token_path: Path = REFRESH_TOKEN_FILE,
        timeout: float = 12.0,
        connection_factory: ConnectionFactory = http.client.HTTPSConnection,
    ) -> None:
        self.api_key_path = api_key_path
        self.device_id_path = device_id_path
        self.access_token_path = access_token_path
        self.refresh_token_path = refresh_token_path
        self.timeout = timeout
        self.connection_factory = connection_factory

    def login(self, email: str, password: str) -> dict[str, Any]:
        email = str(email or "").strip()
        password = str(password or "")
        if not email or "@" not in email or len(email) > 320:
            raise ValueError("md_login_invalid_email")
        if not password or len(password) > 4096:
            raise ValueError("md_login_invalid_password")
        api_key = _load_private_text(self.api_key_path, max_bytes=512)
        device_id = ensure_device_id(self.device_id_path)
        body = json.dumps(
            {"email": email, "password": password},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = self.connection_factory(AUTH_HOST, 443, timeout=self.timeout)
        try:
            connection.request(
                "POST",
                LOGIN_PATH,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "apikey": api_key,
                    "id_dispositivo": device_id,
                    "Sistemaoperativo": "Android",
                    "BuildVersion": BUILD_VERSION,
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("md_login_response_too_large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("md_login_invalid_response") from exc
        if not isinstance(value, Mapping):
            raise RuntimeError("md_login_invalid_response")
        if response.status < 200 or response.status >= 300:
            message = str(value.get("messaggio") or f"HTTP {response.status}")
            raise MdLoginRejected(message[:240])
        payload = _login_payload(value)
        access_token = str(payload.get("token") or "").strip()
        refresh_token = str(payload.get("refreshToken") or "").strip()
        if not access_token:
            raise RuntimeError("md_login_access_token_missing")
        _write_private_text(self.access_token_path, access_token + "\n")
        if refresh_token:
            _write_private_text(self.refresh_token_path, refresh_token + "\n")
        return {
            "ok": True,
            "access_token_saved": True,
            "refresh_token_saved": bool(refresh_token),
            "credentials_saved": False,
        }

    def refresh(self) -> dict[str, Any]:
        api_key = _load_private_text(self.api_key_path, max_bytes=512)
        device_id = ensure_device_id(self.device_id_path)
        refresh_token = _load_private_text(self.refresh_token_path, max_bytes=16384)
        body = json.dumps(
            {"refresh_token": refresh_token},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = self.connection_factory(AUTH_HOST, 443, timeout=self.timeout)
        try:
            connection.request(
                "POST",
                REFRESH_PATH,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "apikey": api_key,
                    "id_dispositivo": device_id,
                    "Sistemaoperativo": "Android",
                    "BuildVersion": BUILD_VERSION,
                },
            )
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("md_refresh_response_too_large")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("md_refresh_invalid_response") from exc
        if not isinstance(value, Mapping):
            raise RuntimeError("md_refresh_invalid_response")
        if response.status < 200 or response.status >= 300:
            raise MdLoginRejected(str(value.get("messaggio") or f"HTTP {response.status}")[:240])
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            raise RuntimeError("md_refresh_payload_missing")
        access_token = str(payload.get("access_token") or payload.get("token") or "").strip()
        new_refresh_token = str(payload.get("refresh_token") or payload.get("refreshToken") or "").strip()
        if not access_token:
            raise RuntimeError("md_refresh_access_token_missing")
        _write_private_text(self.access_token_path, access_token + "\n")
        if new_refresh_token:
            _write_private_text(self.refresh_token_path, new_refresh_token + "\n")
        return {
            "ok": True,
            "access_token_saved": True,
            "refresh_token_saved": bool(new_refresh_token),
            "credentials_saved": False,
        }
