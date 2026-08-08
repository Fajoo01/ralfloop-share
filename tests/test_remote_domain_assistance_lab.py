from types import SimpleNamespace

from ralfloop_agent.inference_lab.remote_domain_assistance import (
    AdvisoryDomainResolver,
    RemoteDomainAssistanceConfig,
    sanitize_domain_advice,
)


class Resolver:
    def resolve(self, goal, context=None):
        return SimpleNamespace(to_dict=lambda: {"status": "resolved", "domain_id": "local_y"})


def test_resolver_precedence_and_no_state_change():
    wrapper = AdvisoryDomainResolver(
        local_resolver=Resolver(),
        remote_call=lambda *_: {"domain_candidates": ["remote_x"], "confidence": 1.0},
        config=RemoteDomainAssistanceConfig(True),
    )
    result = wrapper.resolve("synthetic")
    assert result["resolution"]["domain_id"] == "local_y"
    assert result["resolver_precedence"] == "local_deterministic"
    assert result["domain_state_changed"] is False
    assert result["promotion_requested"] is False


def test_remote_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RALF_REMOTE_DOMAIN_ASSISTANCE_ENABLED", raising=False)
    assert RemoteDomainAssistanceConfig.from_env().enabled is False


def test_binding_advice_rejected():
    try:
        sanitize_domain_advice({"facts": ["auto_approve"]})
    except ValueError as exc:
        assert "binding" in str(exc)
    else:
        raise AssertionError("binding advice accepted")
