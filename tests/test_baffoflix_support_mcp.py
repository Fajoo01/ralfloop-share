from __future__ import annotations

from typing import Any

from ralfloop_agent.integration.jellyfin_identity_mcp_server import JellyfinMCPServer


class FakeResponse:
    def __init__(self, payload: Any, *, content: bytes = b"{}") -> None:
        self._payload = payload
        self.content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


def test_access_info_exposes_public_only(monkeypatch):
    server = JellyfinMCPServer(
        "http://10.0.0.5:8096",
        "super-secret-token",
        public_url="http://public.example:8096",
        landing_url="https://example.org/b/",
    )

    def fake_get(url, **kwargs):
        assert url == "http://public.example:8096/System/Info/Public"
        assert "X-Emby-Token" not in kwargs.get("headers", {})
        return FakeResponse({
            "LocalAddress": "http://10.0.0.5:8096",
            "ServerName": "Baffoflix",
            "Version": "10.11.6",
            "ProductName": "Jellyfin Server",
            "Id": "server-1",
        })

    monkeypatch.setattr("ralfloop_agent.integration.jellyfin_identity_mcp_server.requests.get", fake_get)
    result = server.call("baffoflix_get_access_info", {})
    payload = result["structuredContent"]

    assert result["isError"] is False
    assert payload["server_url"] == "http://public.example:8096"
    assert payload["landing_url"] == "https://example.org/b"
    assert payload["public_health"]["server_name"] == "Baffoflix"
    assert "10.0.0.5" not in repr(payload)
    assert "super-secret-token" not in repr(payload)


def test_password_recovery_requires_confirmation_and_hides_pin_file(monkeypatch):
    server = JellyfinMCPServer("http://jellyfin.internal", "token")
    called = []

    def fake_post(url, **kwargs):
        called.append((url, kwargs))
        return FakeResponse({
            "Action": "PinCode",
            "PinFile": "/config/passwordreset-secret.json",
            "PinExpirationDate": "2026-09-23T20:00:00Z",
        })

    monkeypatch.setattr("ralfloop_agent.integration.jellyfin_identity_mcp_server.requests.post", fake_post)

    denied = server.call(
        "baffoflix_start_password_recovery",
        {"username": "socio", "confirm": False},
    )
    assert denied["isError"] is True
    assert denied["structuredContent"]["status"] == "CONFIRMATION_REQUIRED"
    assert called == []

    started = server.call(
        "baffoflix_start_password_recovery",
        {"username": "socio", "confirm": True},
    )
    payload = started["structuredContent"]
    assert started["isError"] is False
    assert payload["status"] == "STARTED"
    assert payload["pin_required"] is True
    assert payload["pin_file_exposed"] is False
    assert "PinFile" not in repr(payload)
    assert "/config/" not in repr(payload)

    url, kwargs = called[0]
    assert url == "http://jellyfin.internal/Users/ForgotPassword"
    assert kwargs["json"] == {"EnteredUsername": "socio"}
    assert "X-Emby-Token" not in kwargs["headers"]


def test_quick_connect_denies_admin_and_disabled_without_authorizing(monkeypatch):
    server = JellyfinMCPServer("http://jellyfin.internal", "token")
    posts = []
    monkeypatch.setattr(
        "ralfloop_agent.integration.jellyfin_identity_mcp_server.requests.post",
        lambda *args, **kwargs: posts.append((args, kwargs)),
    )

    user_id = "a" * 32
    monkeypatch.setattr(server, "get", lambda path, params=None: {
        "Id": user_id,
        "Name": "admin",
        "Policy": {"IsAdministrator": True, "IsDisabled": False},
    })
    admin = server.call(
        "baffoflix_authorize_quick_connect",
        {"code": "ABC123", "user_id": user_id, "confirm": True},
    )
    assert admin["structuredContent"]["status"] == "ADMIN_ACCOUNT_DENIED"
    assert posts == []

    monkeypatch.setattr(server, "get", lambda path, params=None: {
        "Id": user_id,
        "Name": "disabled",
        "Policy": {"IsAdministrator": False, "IsDisabled": True},
    })
    disabled = server.call(
        "baffoflix_authorize_quick_connect",
        {"code": "ABC123", "user_id": user_id, "confirm": True},
    )
    assert disabled["structuredContent"]["status"] == "ACCOUNT_DISABLED"
    assert posts == []


def test_quick_connect_authorizes_exact_enabled_non_admin(monkeypatch):
    server = JellyfinMCPServer("http://jellyfin.internal", "token")
    user_id = "b" * 32
    monkeypatch.setattr(server, "get", lambda path, params=None: {
        "Id": user_id,
        "Name": "socio",
        "Policy": {"IsAdministrator": False, "IsDisabled": False},
    })
    seen = []

    def fake_post(url, **kwargs):
        seen.append((url, kwargs))
        return FakeResponse(True)

    monkeypatch.setattr("ralfloop_agent.integration.jellyfin_identity_mcp_server.requests.post", fake_post)
    outcome = server.call(
        "baffoflix_authorize_quick_connect",
        {"code": "ABC123", "user_id": user_id, "confirm": True},
    )
    payload = outcome["structuredContent"]
    assert outcome["isError"] is False
    assert payload == {
        "ok": True,
        "status": "AUTHORIZED",
        "user_id": user_id,
        "username": "socio",
    }
    url, kwargs = seen[0]
    assert url == "http://jellyfin.internal/QuickConnect/Authorize"
    assert kwargs["params"] == {"Code": "ABC123", "UserId": user_id}
    assert kwargs["headers"]["X-Emby-Token"] == "token"
    assert "AccessToken" not in repr(payload)


def test_baffoflix_mutations_are_confirmation_gated():
    server = JellyfinMCPServer("http://jellyfin.invalid", "token")
    tools = {item["name"]: item for item in server.list_tools()}
    for name in ("baffoflix_start_password_recovery", "baffoflix_authorize_quick_connect"):
        confirm = tools[name]["inputSchema"]["properties"]["confirm"]
        assert confirm == {"type": "boolean", "const": True}
        assert "confirm" in tools[name]["inputSchema"]["required"]
