from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests


def auth_mode() -> str:
    return os.getenv("BOTTAZZI_APP_AUTH_MODE", "password").strip().casefold()


def enabled() -> bool:
    return auth_mode() == "oidc"


def client_id() -> str:
    return os.getenv("BOTTAZZI_APP_OIDC_CLIENT_ID", "tiremm-bottazzi").strip()


def client_secret() -> str:
    path = os.getenv("BOTTAZZI_APP_OIDC_CLIENT_SECRET_FILE", "").strip()
    if not path:
        credentials_dir = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
        if credentials_dir:
            path = str(Path(credentials_dir) / "oidc-client-secret")
    if path:
        return Path(path).read_text(encoding="utf-8").strip()
    return os.getenv("BOTTAZZI_APP_OIDC_CLIENT_SECRET", "").strip()


def discovery_url() -> str:
    return os.getenv("BOTTAZZI_APP_OIDC_DISCOVERY_URL", "").strip()


def redirect_uri() -> str:
    return os.getenv("BOTTAZZI_APP_OIDC_REDIRECT_URI", "").strip()


def discovery(timeout: int = 8) -> dict[str, Any]:
    url = discovery_url()
    if not url.startswith("https://"):
        raise RuntimeError("oidc_discovery_url_invalid")
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        if not str(payload.get(key) or "").startswith("https://"):
            raise RuntimeError(f"oidc_{key}_invalid")
    return payload


def authorization_url(*, state: str, timeout: int = 8) -> str:
    cfg = discovery(timeout=timeout)
    params = {
        "client_id": client_id(),
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid profile email",
        "state": state,
    }
    return str(cfg["authorization_endpoint"]) + "?" + urlencode(params)


def exchange_code(code: str, *, timeout: int = 10) -> dict[str, Any]:
    cfg = discovery(timeout=timeout)
    secret = client_secret()
    if len(secret) < 32:
        raise RuntimeError("oidc_client_secret_missing")
    response = requests.post(
        str(cfg["token_endpoint"]),
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri(),
            "client_id": client_id(),
            "client_secret": secret,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    token = response.json()
    access_token = str(token.get("access_token") or "")
    if not access_token:
        raise RuntimeError("oidc_access_token_missing")
    userinfo = requests.get(
        str(cfg["userinfo_endpoint"]),
        headers={"authorization": f"Bearer {access_token}"},
        timeout=timeout,
    )
    userinfo.raise_for_status()
    identity = userinfo.json()
    if not str(identity.get("sub") or ""):
        raise RuntimeError("oidc_subject_missing")
    return identity
