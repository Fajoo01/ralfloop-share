from pathlib import Path

from ralfloop_agent.providers.gpu_engine_scheduler import ExternalEngineConfig, ExternalEngineLifecycle
from ralfloop_agent.providers.llama_cpp_server import ProcessIdentity


class Client:
    def status(self):
        return {"ok": True, "action": "status", "active": True, "main_pid": 42,
                "port_19093": True, "model": "AgentCPM-Explore"}


def test_agentcpm_matching_uses_configured_cross_uid(tmp_path):
    model = tmp_path / "agentcpm.gguf"
    config = ExternalEngineConfig(model_path=model, expected_uid=1001)
    lifecycle = ExternalEngineLifecycle(config, client=Client())
    argv = ("llama-server", "--port", "19093", "--model", str(model))
    assert lifecycle._matches(ProcessIdentity(42, 1001, 1, "", argv))
    assert not lifecycle._matches(ProcessIdentity(42, 1000, 1, "", argv))


def test_broker_provenance_is_explicit_when_proc_hidden(tmp_path):
    lifecycle = ExternalEngineLifecycle(ExternalEngineConfig(model_path=tmp_path / "m"),
        client=Client(), identity_reader=lambda _pid: None)
    provenance = lifecycle.discover()
    assert provenance.manager == "broker_systemd_user"
    assert provenance.unit == "ralfloop-agentcpm.service"
    assert provenance.owner_uid == 1001
