from __future__ import annotations

import json
from pathlib import Path

from ralfloop_agent.unified_assistant.md_goodify_auth import MdGoodifyAuthenticator


class FakeResponse:
    status = 200

    def read(self, _limit):
        return json.dumps({
            "payload": {"token": "access-abc", "refreshToken": "refresh-def"}
        }).encode("utf-8")


class FakeConnection:
    def __init__(self, host, port, *, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.request_args = None
        self.closed = False

    def request(self, method, path, *, body, headers):
        self.request_args = (method, path, body, headers)

    def getresponse(self):
        return FakeResponse()

    def close(self):
        self.closed = True


def _private(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)
    return path


def test_login_matches_apk_contract_and_persists_only_tokens(tmp_path: Path):
    api_key = _private(tmp_path / "api-key", "apk-key")
    device_id = _private(tmp_path / "device-id", "0123456789abcdef")
    access = tmp_path / "access-token"
    refresh = tmp_path / "refresh-token"
    connections = []

    def factory(host, port, *, timeout):
        conn = FakeConnection(host, port, timeout=timeout)
        connections.append(conn)
        return conn

    auth = MdGoodifyAuthenticator(
        api_key_path=api_key,
        device_id_path=device_id,
        access_token_path=access,
        refresh_token_path=refresh,
        connection_factory=factory,
    )
    result = auth.login("fabio@example.test", "password-once")
