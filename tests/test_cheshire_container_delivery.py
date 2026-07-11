import hashlib
import importlib
import inspect
from pathlib import Path


def test_plugin_active_baseline_sha_unchanged():
    plugin = Path("/home/sibilla-cumana/gatto/cat/plugins/ralfloop_bridge/main_plugin.py")
    assert hashlib.sha256(plugin.read_bytes()).hexdigest() == "ea941a7d8d33843fc8829561af72aa9e82129262c7f61ef8b95f7a153c1d21a6"


def test_hook_feature_off_does_not_import_bridge(monkeypatch):
    import sys

    monkeypatch.delenv("RALFLOOP_ENABLE_DOMAIN_ORCHESTRATOR", raising=False)
    sys.modules.pop("ralfloop_agent.integration.cheshire_domain_bridge", None)
    hook = importlib.import_module("ralfloop_agent.integration.cheshire_plugin_hook")
    result = hook.try_domain_orchestrator_sync("Quanto fa 2 + 3?", {"requested_domain": "arithmetic_basic"})
    assert result["handled"] is False
    assert "ralfloop_agent.integration.cheshire_domain_bridge" not in sys.modules


def test_host_socket_bridge_uses_client_not_local_controller(monkeypatch, tmp_path):
    from ralfloop_agent.integration.cheshire_domain_bridge import CheshireDomainBridge, CheshireDomainBridgeConfig

    class FakeClient:
        calls = []

        def execute(self, request):
            self.calls.append(request)
            return {
                "ok": True,
                "status": "completed",
                "answer": "host answer",
                "native_latent_verified": True,
                "closed_loop_verified": True,
                "selected_backend": "recursive_mas_native",
                "fallback_used": False,
            }

    monkeypatch.setattr("ralfloop_agent.integration.cheshire_domain_bridge.RecursiveMASHostClient.from_env", lambda: FakeClient())
    bridge = CheshireDomainBridge(CheshireDomainBridgeConfig(enabled=True, enable_domain_jury=True, enable_recursive_mas_native=True, audit_enabled=False, recursive_mas_transport="host_socket"))
    result = bridge.execute({"message": "Qual è la strategia più prudente per questo incidente?", "domain": "incident_triage"})
    assert result.status == "completed"
    assert result.answer == "host answer"
    assert FakeClient.calls[0]["domain_context"]["domain_id"] == "incident_triage"


def test_client_and_hook_import_do_not_import_torch(monkeypatch):
    import subprocess
    import sys

    code = (
        "import sys; "
        "import ralfloop_agent.integration.recursive_mas_host_client; "
        "import ralfloop_agent.integration.cheshire_plugin_hook; "
        "print('torch' in sys.modules)"
    )
    out = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    assert out == "False"


def test_systemd_candidate_defaults_exist():
    service = Path("deploy/systemd/ralfloop-recursive-mas.service").read_text(encoding="utf-8")
    env = Path("deploy/systemd/ralfloop-recursive-mas.env.example").read_text(encoding="utf-8")
    assert "User=sibilla-cumana" in service
    assert "RuntimeDirectory=ralfloop" in service
    assert "RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE=0" in env
    assert "RALFLOOP_RECURSIVE_MAS_EXECUTION_MODE=gpu_stagewise" in env


def test_future_plugin_patch_must_be_lazy_import():
    source = inspect.getsource(importlib.import_module("ralfloop_agent.integration.cheshire_plugin_hook"))
    assert "RALFLOOP_ENABLE_DOMAIN_ORCHESTRATOR" in source
    assert "from .cheshire_domain_bridge import" not in source.split("def try_domain_orchestrator_sync", 1)[0]
