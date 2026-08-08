import pytest

from ralfloop_agent.domains.source_discovery import ConfiguredSearchProvider, SearchProviderUnavailable, SearxngSearchProvider, create_search_provider


def test_provider_default_none(monkeypatch):
    monkeypatch.delenv("RALFLOOP_BANDO_SEARCH_PROVIDER", raising=False)
    with pytest.raises(SearchProviderUnavailable, match="search_provider_unavailable"):
        create_search_provider()


def test_provider_searxng_selected_from_env(monkeypatch):
    monkeypatch.setenv("RALFLOOP_BANDO_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("RALFLOOP_SEARXNG_URL", "http://127.0.0.1:8888")
    assert isinstance(create_search_provider(), SearxngSearchProvider)


def test_unsupported_provider(monkeypatch):
    monkeypatch.setenv("RALFLOOP_BANDO_SEARCH_PROVIDER", "bing")
    with pytest.raises(SearchProviderUnavailable, match="unsupported_search_provider"):
        create_search_provider()


def test_endpoint_not_user_configurable_from_request():
    with pytest.raises(SearchProviderUnavailable, match="provider_endpoint_not_allowlisted"):
        SearxngSearchProvider("https://search.example.org")


def test_configured_provider_reports_unavailable_without_env(monkeypatch):
    monkeypatch.delenv("RALFLOOP_BANDO_SEARCH_PROVIDER", raising=False)
    provider = ConfiguredSearchProvider()
    with pytest.raises(SearchProviderUnavailable, match="search_provider_unavailable"):
        provider.search("x", limit=1)
    assert provider.health()["error"] == "search_provider_unavailable"


def test_configured_provider_health_delegates(monkeypatch):
    def boom(*_args, **_kwargs):
        raise ConnectionResetError("reset")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    monkeypatch.setenv("RALFLOOP_BANDO_SEARCH_PROVIDER", "searxng")
    monkeypatch.setenv("RALFLOOP_SEARXNG_URL", "http://127.0.0.1:8888")
    provider = ConfiguredSearchProvider.from_env()
    health = provider.health()
    assert health["provider"] == "searxng"
