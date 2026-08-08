from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ralfloop_agent.domains import source_fetcher
from ralfloop_agent.domains.source_fetcher import SourceFetcher, validate_url


class Handler(BaseHTTPRequestHandler):
    body = b"hello"
    content_type = "text/plain"
    redirect = False

    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/final")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *_args):
        return


@pytest.fixture
def http_server():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        Handler.body = b"hello"
        Handler.content_type = "text/plain"


def test_ssrf_localhost_blocked():
    assert validate_url("http://127.0.0.1/test") == "ssrf_blocked"
    assert validate_url("http://localhost/test") == "ssrf_blocked"


def test_private_ip_blocked():
    assert validate_url("http://10.0.0.1/test") == "ssrf_blocked"


def test_executable_suffix_blocked():
    out = SourceFetcher().fetch("https://example.org/tool.exe")
    assert out.status == "executable_or_archive_blocked"


def test_fetch_allowed_content_checksum_cache(tmp_path, monkeypatch, http_server):
    monkeypatch.setattr(source_fetcher, "validate_url", lambda _url: None)
    out = SourceFetcher(cache_dir=tmp_path).fetch(http_server + "/doc.txt")
    assert out.ok is True
    assert out.checksum
    assert (tmp_path / out.checksum / "metadata.json").exists()
    assert (tmp_path / out.checksum / "headers-redacted.json").exists()
    assert (tmp_path / out.checksum / "assessment.json").exists()


def test_redirect_recorded(tmp_path, monkeypatch, http_server):
    monkeypatch.setattr(source_fetcher, "validate_url", lambda _url: None)
    out = SourceFetcher(cache_dir=tmp_path).fetch(http_server + "/redirect")
    assert out.ok is True
    assert out.redirect_chain


def test_mime_blocked(tmp_path, monkeypatch, http_server):
    monkeypatch.setattr(source_fetcher, "validate_url", lambda _url: None)
    Handler.content_type = "application/octet-stream"
    out = SourceFetcher(cache_dir=tmp_path).fetch(http_server + "/bad")
    assert out.status == "mime_not_allowed"


def test_file_too_large(tmp_path, monkeypatch, http_server):
    monkeypatch.setattr(source_fetcher, "validate_url", lambda _url: None)
    Handler.body = b"x" * 20
    out = SourceFetcher(cache_dir=tmp_path, max_bytes=5).fetch(http_server + "/large")
    assert out.status == "file_too_large"
