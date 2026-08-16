from __future__ import annotations

import pytest

from src.mcp_client import MCPClient


def test_legacy_email_client_never_reads_oauth_files_as_fallback(monkeypatch):
    monkeypatch.setenv("RALF_MCP_GOOGLE_ENABLED", "1")
    monkeypatch.setenv("RALF_GOOGLE_CLIENT_SECRETS", "/forbidden/client.json")
    monkeypatch.setenv("RALF_GOOGLE_TOKEN", "/forbidden/token.json")

    client = MCPClient()

    assert client.google is None
    with pytest.raises(NotImplementedError, match="not configured"):
        client.send_email("user@example.invalid", "Subject", "Body")
