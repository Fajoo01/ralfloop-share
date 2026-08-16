from __future__ import annotations

import os
from pathlib import Path
from urllib.request import urlopen

import pytest

from ralfloop_agent.unified_assistant.home import HomeEntityRegistry
from ralfloop_agent.unified_assistant.home_provider import HomeAssistantRESTBackend
from ralfloop_agent.unified_assistant.memory import MemoryRouter, tiremm_profile_items
from ralfloop_agent.unified_assistant.registry import DEFAULT_HOME_ENTITIES, UnifiedRegistryFacade
from src.google_workspace import GoogleWorkspaceGateway
from src.mcp_transport import MCPClientSession, UnixMCPTransport


pytestmark = pytest.mark.integration


def _enabled():
    if os.getenv("RALFLOOP_UNIFIED_LIVE_READ_TEST") != "1":
        pytest.skip("set RALFLOOP_UNIFIED_LIVE_READ_TEST=1 for read-only live smoke")


def test_live_home_inventory_read_only():
    _enabled()
    backend = HomeAssistantRESTBackend.from_environment()
    observed = {item["entity_id"] for item in backend.list_entities()}
    configured = set(HomeEntityRegistry.load(DEFAULT_HOME_ENTITIES).by_id)

    assert configured <= observed
    assert backend.health()["ok"]


def test_live_gmail_registry_memory_and_agentcpm_read_only():
    _enabled()
    registry = UnifiedRegistryFacade()
    assert registry.domain("email").id == "email"
    profile = Path(__file__).parents[1] / "config" / "reply_context_profiles.json"
    memory = MemoryRouter(tiremm_profile_items(profile))
    assert memory.retrieve(
        registry.domain("email"), requested_namespaces=("tiremm",), limit=2
    ).trace.active_domain == "email"
    socket_path = os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
    account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
    with MCPClientSession(UnixMCPTransport(socket_path), timeout=30) as session:
        gateway = GoogleWorkspaceGateway(session, account=account)
        gateway.discover()
        assert isinstance(gateway.invoke("search", query="Magnolia", maxResults=1), dict)
    with urlopen("http://127.0.0.1:19093/health", timeout=10) as response:
        assert response.status == 200
