from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ralfloop_agent.domains.source_discovery import SearchProviderUnavailable, SearxngSearchProvider


class SearxHandler(BaseHTTPRequestHandler):
    status = 200
    payload = {"results": [{"title": "ACT", "url": "https://www.fondazioneunipolis.org/a.pdf", "content": "snippet", "engine": "duckduckgo", "score": 1.0}]}
    seen_path = ""

    def do_GET(self):
        SearxHandler.seen_path = self.path
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        if isinstance(self.payload, bytes):
            self.wfile.write(self.payload)
        else:
            self.wfile.write(json.dumps(self.payload).encode())

    def log_message(self, *_args):
        return


@pytest.fixture
def searx_server():
    SearxHandler.status = 200
    SearxHandler.payload = {"results": [{"title": "ACT", "url": "https://www.fondazioneunipolis.org/a.pdf", "content": "snippet", "engine": "duckduckgo", "score": 1.0}]}
    server = HTTPServer(("127.0.0.1", 0), SearxHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_searxng_query_encoded_and_parsed(searx_server):
    provider = SearxngSearchProvider(searx_server)
    out = provider.search("Fondazione Unipolis ACT 2026", limit=5)
    assert out[0].title == "ACT"
    assert out[0].metadata["provider"] == "searxng"
    assert "q=Fondazione+Unipolis+ACT+2026" in SearxHandler.seen_path
    assert "format=json" in SearxHandler.seen_path


def test_searxng_explicit_engines_are_sent(searx_server):
    provider = SearxngSearchProvider(searx_server, engines="bing,startpage")

    provider.search("bandi Milano", limit=2)

    assert "engines=bing%2Cstartpage" in SearxHandler.seen_path
    assert provider.health()["engines"] == "bing,startpage"


def test_searxng_health(searx_server):
    health = SearxngSearchProvider(searx_server).health()
    assert health["provider"] == "searxng"
    assert health["configured"] is True
    assert health["json_enabled"] is True


def test_searxng_403_json_disabled(searx_server):
    SearxHandler.status = 403
    with pytest.raises(SearchProviderUnavailable, match="provider_json_format_disabled"):
        SearxngSearchProvider(searx_server).search("x", limit=1)


def test_searxng_429_rate_limited(searx_server):
    SearxHandler.status = 429
    with pytest.raises(SearchProviderUnavailable, match="provider_rate_limited"):
        SearxngSearchProvider(searx_server).search("x", limit=1)


def test_searxng_invalid_json(searx_server):
    SearxHandler.payload = b"{bad"
    with pytest.raises(SearchProviderUnavailable, match="provider_invalid_json"):
        SearxngSearchProvider(searx_server).search("x", limit=1)


def test_searxng_empty_result(searx_server):
    SearxHandler.payload = {"results": []}
    with pytest.raises(SearchProviderUnavailable, match="provider_empty_result"):
        SearxngSearchProvider(searx_server).search("x", limit=1)


def test_connection_reset_is_provider_unavailable(monkeypatch, searx_server):
    def boom(*_args, **_kwargs):
        raise ConnectionResetError("reset")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    with pytest.raises(SearchProviderUnavailable, match="provider_unavailable"):
        SearxngSearchProvider(searx_server).search("x", limit=1)
