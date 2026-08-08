import importlib
import asyncio
import sys

from ralfloop_agent.integration.cheshire_plugin_hook import try_domain_orchestrator


def test_hook_feature_off_leaves_legacy(monkeypatch):
    monkeypatch.delenv("RALFLOOP_ENABLE_DOMAIN_ORCHESTRATOR", raising=False)
    result = asyncio.run(try_domain_orchestrator({"text": "Quanto fa 2 + 3?"}, {"requested_domain": "arithmetic_basic"}))
    assert result["handled"] is False
    assert result["fallback_reason"] == "domain_orchestrator_disabled"


def test_hook_deterministic_handles_once(monkeypatch):
    monkeypatch.setenv("RALFLOOP_ENABLE_DOMAIN_ORCHESTRATOR", "1")
    monkeypatch.setenv("RALFLOOP_ENABLE_DOMAIN_JURY", "0")
    result = asyncio.run(try_domain_orchestrator({"text": "Quanto fa 2 + 3?"}, {"requested_domain": "arithmetic_basic"}))
    assert result["handled"] is True
    assert result["answer"] == "5"
    assert result["source"] == "domain_deterministic"


def test_hook_missing_domain(monkeypatch):
    monkeypatch.setenv("RALFLOOP_ENABLE_DOMAIN_ORCHESTRATOR", "1")
    result = asyncio.run(try_domain_orchestrator("Valuta un dominio non registrato", {}))
    assert result["handled"] is True
    assert result["domain_creation_required"] is True


def test_hook_import_does_not_import_torch_or_load_models(monkeypatch):
    sys.modules.pop("ralfloop_agent.integration.cheshire_plugin_hook", None)
    sys.modules.pop("torch", None)
    importlib.import_module("ralfloop_agent.integration.cheshire_plugin_hook")
    assert "torch" not in sys.modules


def test_main_plugin_sha_not_modified():
    import hashlib
    from pathlib import Path

    plugin = Path("/home/sibilla-cumana/gatto/cat/plugins/ralfloop_bridge/main_plugin.py")
    assert hashlib.sha256(plugin.read_bytes()).hexdigest() == "ea941a7d8d33843fc8829561af72aa9e82129262c7f61ef8b95f7a153c1d21a6"
