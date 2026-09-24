from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from openshell_backend import app_gateway


def _password_spec(password: str) -> str:
    salt = bytes.fromhex("00112233445566778899aabbccddeeff")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000).hex()
    return f"pbkdf2_sha256$200000${salt.hex()}${digest}"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("BOTTAZZI_APP_PASSWORD_HASH", _password_spec("app-pass"))
    monkeypatch.setenv("BOTTAZZI_APP_SESSION_SECRET", "test-session-secret")
    return TestClient(app_gateway.app)


def test_app_requires_auth_and_uses_bot_tazzi_cookie(client: TestClient) -> None:
    assert client.get("/", follow_redirects=False, headers={"accept": "text/html"}).status_code == 303
    assert client.get("/assistant/v1/status").json()["detail"] == "app_auth_required"
    login = client.post("/login", json={"password": "app-pass"})
    assert login.status_code == 200
    assert "bottazzi_app_session=" in login.headers["set-cookie"]


def test_bot_tazzi_is_product_and_peppone_is_persona(client: TestClient) -> None:
    client.post("/login", json={"password": "app-pass"})
    ui = client.get("/")
    assert ui.status_code == 200
    assert '<body data-app-gateway="1">' in ui.text
    assert "<title>Bot-tazzi — Peppone</title>" in ui.text
    assert "<strong>Bot-tazzi</strong>" in ui.text
    assert "Peppone · Assistente autonomo locale" in ui.text
    assert "<h1>Bot-tazzi</h1>" in ui.text
    assert "Scrivi a Bot-tazzi" in ui.text
    assert "<strong>Peppone</strong>" not in ui.text
    manifest = client.get("/manifest.webmanifest").json()
    assert manifest["short_name"] == "Bot-tazzi"
    assert manifest["name"].startswith("Bot-tazzi")


def test_app_internet_agent_stays_gateway_bounded(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/login", json={"password": "app-pass"})
    seen = {}

    class FakeResponse:
        status_code = 200
        content = b'{"ok":true,"response":"grounded"}'
        headers = {"content-type": "application/json"}
        ok = True

    def fake_post(url, *, json, timeout):
        seen.update(json)
        return FakeResponse()

    monkeypatch.setattr(app_gateway.requests, "post", fake_post)
    response = client.post("/assistant/v1/chat", json={"message": "QUIC?", "allow_tools": False, "app_internet_agent": True})
    assert response.status_code == 200
    assert seen["allow_tools"] is True
    assert "app_internet_agent" not in seen
    assert seen["context"]["app_internet_agent"] is True


def test_task_queue_proxy_uses_authenticated_gateway(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client.post("/login", json={"password": "app-pass"})
    seen = {}

    class FakeResponse:
        status_code = 200
        content = b"{\"count\":0,\"tasks\":[]}"
        headers = {"content-type": "application/json"}
        ok = True

    def fake_request(method, url, *, params, data, headers, timeout):
        seen.update(method=method, url=url, params=params, data=data, headers=headers, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(app_gateway.requests, "request", fake_request)
    response = client.get("/assistant/v1/tasks")
    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert seen["method"] == "GET"
    assert seen["url"].endswith("/assistant/v1/tasks")


def test_gpt_queue_proxy_is_authenticated_and_forwards_attachment(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    assert client.get("/assistant/v1/gpt").status_code == 401
    client.post("/login", json={"password": "app-pass"})
    seen = {}

    class FakeResponse:
        status_code = 200
        content = b'{"ok":true,"attached":true}'
        headers = {"content-type": "application/json"}
        ok = True

    def fake_request(method, url, *, params, data, headers, timeout):
        seen.update(method=method, url=url, params=params, data=data, headers=headers, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(app_gateway.requests, "request", fake_request)
    response = client.post(
        "/assistant/v1/gpt/jobs/job-1/attachment",
        content=b"photo",
        headers={
            "content-type": "image/jpeg",
            "x-bottazzi-file-name": "foto.jpg",
            "x-bottazzi-image": "1",
        },
    )
    assert response.status_code == 200
    assert seen["url"].endswith("/api/jobs/job-1/attachment")
    assert seen["headers"]["X-Bottazzi-Frontend"] == "1"
    assert seen["headers"]["x-bottazzi-file-name"] == "foto.jpg"
    assert seen["headers"]["x-bottazzi-image"] == "1"
    assert seen["data"] == b"photo"


def test_android_shell_has_no_source_hardcoded_backend() -> None:
    root = Path(__file__).resolve().parents[1] / "android" / "bottazzi-app"
    gradle = (root / "app" / "build.gradle").read_text(encoding="utf-8")
    activity = (root / "app" / "src" / "main" / "java" / "org" / "tiremminnanz" / "bottazzi" / "MainActivity.java").read_text(encoding="utf-8")
    manifest = (root / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert "BOTTAZZI_APP_URL" in gradle
    assert "BuildConfig.APP_URL" in activity
    assert "19090" not in activity
    assert 'android:label="Bot-tazzi"' in manifest
    assert 'android.permission.RECORD_AUDIO' in manifest
    assert 'PermissionRequest.RESOURCE_AUDIO_CAPTURE' in activity
    assert 'sameOrigin(BuildConfig.APP_URL, request.getOrigin())' in activity


def test_gateway_systemd_is_release_bound_and_secret_free() -> None:
    root = Path(__file__).resolve().parents[1]
    unit = (root / "deploy" / "systemd" / "ralf-bottazzi-app-gateway.service").read_text(encoding="utf-8")
    example = (root / "deploy" / "systemd" / "bottazzi-app-gateway.env.example").read_text(encoding="utf-8")
    assert "/ralfloop-production/current" in unit
    assert "EnvironmentFile=/etc/ralfloop/bottazzi-app-gateway.env" in unit
    assert "REPLACE_WITH_RANDOM_SECRET" in example
    assert "BOTTAZZI_APP_PASSWORD_HASH=" in example
