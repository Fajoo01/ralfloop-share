from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlencode

import openshell_backend.app as backend_app


async def _request_inprocess(
    method: str,
    path: str,
    *,
    query: str = "",
    headers: list[tuple[bytes, bytes]] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    sent = False
    messages: list[dict] = []

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("utf-8"),
        "root_path": "",
        "headers": headers or [],
        "client": ("testclient", 123),
        "server": ("testserver", 80),
    }

    await backend_app.app(scope, receive, send)

    start = next(message for message in messages if message["type"] == "http.response.start")
    response_headers = {
        key.decode("latin1").lower(): value.decode("latin1")
        for key, value in start.get("headers", [])
    }
    chunks = [message.get("body", b"") for message in messages if message["type"] == "http.response.body"]
    return start["status"], response_headers, b"".join(chunks)


def _build_multipart(
    boundary: str,
    *,
    fields: dict[str, str],
    files: list[tuple[str, str, bytes, str]],
) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    for field_name, filename, content, content_type in files:
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
            + content
            + b"\r\n"
        )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts)


def test_pending_skill_manual_upload_and_list(tmp_path, monkeypatch) -> None:
    pending_root = tmp_path / "skills_pending"
    monkeypatch.setattr(backend_app, "PENDING_SKILLS_DIR", pending_root)

    body = _build_multipart(
        "boundary-upload-1",
        fields={"skill_name": "Parser Bilancio 2026"},
        files=[
            ("manual_files", "Manuale Uno.pdf", b"pdf-one", "application/pdf"),
            ("manual_files", "../Guida?.md", b"guida-due", "text/markdown"),
        ],
    )
    status, headers, _ = asyncio.run(
        _request_inprocess(
            "POST",
            "/pending-skills/manuals/upload",
            headers=[(b"content-type", b"multipart/form-data; boundary=boundary-upload-1")],
            body=body,
        )
    )

    assert status == 303
    assert "location" in headers
    parsed = parse_qs(headers["location"].split("?", 1)[1])
    assert parsed["skill_name"] == ["parser-bilancio-2026"]

    manual_dir = pending_root / "parser-bilancio-2026" / "manuals"
    assert (manual_dir / "Manuale_Uno.pdf").read_bytes() == b"pdf-one"
    assert (manual_dir / "Guida_.md").read_bytes() == b"guida-due"

    status, _, body = asyncio.run(
        _request_inprocess(
            "GET",
            "/pending-skills/manuals/list",
            query=urlencode({"skill_name": "Parser Bilancio 2026"}),
        )
    )
    payload = json.loads(body.decode("utf-8"))

    assert status == 200
    assert payload == {
        "ok": True,
        "skill_name": "parser-bilancio-2026",
        "manuals": ["Guida_.md", "Manuale_Uno.pdf"],
        "storage_dir": str(manual_dir),
    }

    status, _, body = asyncio.run(
        _request_inprocess(
            "GET",
            "/pending-skills/manuals",
            query=urlencode({"skill_name": "Parser Bilancio 2026"}),
        )
    )
    html = body.decode("utf-8")

    assert status == 200
    assert "Manuali skill pending" in html
    assert "Manuale_Uno.pdf" in html
    assert "Guida_.md" in html


def test_pending_skill_manual_upload_variant_sanitizes_skill_and_filename(tmp_path, monkeypatch) -> None:
    pending_root = tmp_path / "skills_pending"
    monkeypatch.setattr(backend_app, "PENDING_SKILLS_DIR", pending_root)

    body = _build_multipart(
        "boundary-upload-2",
        fields={"skill_name": " ../../Nuova Skill++ "},
        files=[
            ("manual_files", "A B.txt", b"variant-content", "text/plain"),
        ],
    )
    status, headers, _ = asyncio.run(
        _request_inprocess(
            "POST",
            "/pending-skills/manuals/upload",
            headers=[(b"content-type", b"multipart/form-data; boundary=boundary-upload-2")],
            body=body,
        )
    )

    assert status == 303
    parsed = parse_qs(headers["location"].split("?", 1)[1])
    assert parsed["skill_name"] == ["nuova-skill"]
    assert (pending_root / "nuova-skill" / "manuals" / "A_B.txt").read_bytes() == b"variant-content"
