from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ralfloop_agent.unified_assistant.md_goodify_readonly import (
    GET_DONATION_PATH,
    GOODIFY_HOST,
    MdGoodifyReadOnlyClient,
    load_access_token,
)


class FakeResponse:
    status = 200

    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def read(self, _limit: int) -> bytes:
        return self.payload


class FakeConnection:
    def __init__(self, host: str, port: int, *, timeout: float):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.request_args = None
        self.closed = False

    def request(self, method, path, *, body, headers):
        self.request_args = (method, path, body, headers)

    def getresponse(self):
        return FakeResponse({
            "code": "",
            "messaggio": "ok",
            "payload": [{"Donation": [{
                "Goodify_associationName": "Tiremm Innanz APS",
                "Goodify_donationId": "d-1",
            }]}],
        })

    def close(self):
        self.closed = True


def _token_file(tmp_path: Path) -> Path:
    path = tmp_path / "access-token"
    path.write_text("secret-access-token\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_token_file_is_private_and_not_symlinked(tmp_path: Path):
    path = _token_file(tmp_path)
    assert load_access_token(path) == "secret-access-token"
    path.chmod(0o640)
    with pytest.raises(RuntimeError, match="permissions"):
        load_access_token(path)


def test_missing_token_fails_closed(tmp_path: Path):
    with pytest.raises(RuntimeError, match="token_unavailable"):
        load_access_token(tmp_path / "missing")


def test_get_donations_uses_only_exact_read_endpoint(tmp_path: Path):
    connections: list[FakeConnection] = []

    def factory(host: str, port: int, *, timeout: float):
        conn = FakeConnection(host, port, timeout=timeout)
        connections.append(conn)
        return conn

    client = MdGoodifyReadOnlyClient(
        token_path=_token_file(tmp_path),
        connection_factory=factory,
    )
    result = client.get_donations()
    conn = connections[0]
    method, path, body, headers = conn.request_args
    assert (conn.host, conn.port) == (GOODIFY_HOST, 443)
    assert (method, path) == ("POST", GET_DONATION_PATH)
    assert json.loads(body) == {"token": "secret-access-token"}
    assert headers["Content-Type"] == "application/json"
    assert conn.closed is True
    assert result["donations"][0]["Goodify_donationId"] == "d-1"
    assert result["network_requests"] == 1
    assert result["mutations"] == 0

    serialized = json.dumps(result)
    assert "secret-access-token" not in serialized


def test_readonly_module_exposes_no_purchase_network_call():
    import ralfloop_agent.unified_assistant.md_goodify_readonly as module

    names = set(dir(module))
    assert "start_donation_flow" not in names
    assert "purchase_donation" not in names
    assert "PURCHASE_DONATION_PATH" not in names
