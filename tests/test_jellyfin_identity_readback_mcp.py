from ralfloop_agent.integration.jellyfin_identity_mcp_server import JellyfinMCPServer


def test_get_identity_is_read_only_and_minimal(monkeypatch):
    server = JellyfinMCPServer("http://jellyfin.invalid", "token")
    seen = []
    def fake_get(path, params=None):
        seen.append((path, dict(params or {})))
        return {"Items": [{
            "Id": "a" * 32, "Name": "Test Film", "ProductionYear": 2024,
            "ProviderIds": {"Tmdb": "123", "Imdb": "tt456"}, "Path": "/secret/path",
        }]}
    monkeypatch.setattr(server, "get", fake_get)
    result = server.call("jellyfin_get_movie_identity", {"item_id": "a" * 32})
    payload = result["structuredContent"]
    assert payload == {
        "ok": True, "status": "FOUND", "item_id": "a" * 32,
        "name": "Test Film", "year": 2024,
        "provider_ids": {"Tmdb": "123", "Imdb": "tt456"},
    }
    assert seen and seen[0][0] == "/Items"
    assert "Path" not in repr(payload)


def test_get_identity_tool_is_declared_without_confirm():
    server = JellyfinMCPServer("http://jellyfin.invalid", "token")
    tool = next(x for x in server.list_tools() if x["name"] == "jellyfin_get_movie_identity")
    assert tool["inputSchema"]["required"] == ["item_id"]
    assert "confirm" not in tool["inputSchema"]["properties"]
