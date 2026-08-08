from dataclasses import replace

from ralfloop_agent.inference_lab.remote_token_speculation import (
    CompatibilityReport,
    RemoteSpeculationConfig,
    RemoteTokenSpeculationLab,
    commit_verified_tokens,
    inspect_llama_external_draft_support,
)
from ralfloop_agent.inference_lab.speculative_metrics import SpeculativeMetrics
from ralfloop_agent.inference_lab.remote_draft_v1_client import CircuitBreaker


def _compat(compatible=True):
    return CompatibilityReport(compatible, "compatible" if compatible else "tokenizer_incompatible", "a", "a", "b", "b", 10, 10, 100, 0)


def test_k_values_and_default_disabled(monkeypatch):
    monkeypatch.delenv("RALF_REMOTE_SPECULATION_ENABLED", raising=False)
    assert RemoteSpeculationConfig.from_env().enabled is False
    for maximum in (1, 2, 4, 8):
        assert RemoteSpeculationConfig(False, maximum).maximum == maximum


def test_no_duplicate_or_lost_tokens():
    committed, accepted = commit_verified_tokens([1, 2, 9, 8], [1, 2, 3, 4])
    assert accepted == 2
    assert committed == [1, 2, 3]


def test_remote_round_and_acceptance_metrics():
    lab = RemoteTokenSpeculationLab(RemoteSpeculationConfig(True, 4))
    metrics = SpeculativeMetrics()
    result = lab.run_round(
        context=[10],
        draft=lambda context, maximum: ([1, 2, 9, 9], 13.0, 120),
        verify=lambda context, proposed: ([1, 2, 3, 4], 4.0),
        compatibility=_compat(),
        metrics=metrics,
    )
    assert result["committed"] == [1, 2, 3]
    assert metrics.acceptance_rate == 0.5


def test_offline_or_incompatible_falls_back_without_tokens():
    lab = RemoteTokenSpeculationLab(RemoteSpeculationConfig(True, 4))
    result = lab.run_round(
        context=[10],
        draft=lambda *_: (_ for _ in ()).throw(AssertionError()),
        verify=lambda *_: (_ for _ in ()).throw(AssertionError()),
        compatibility=_compat(False),
        metrics=SpeculativeMetrics(),
    )
    assert result == {"active": False, "fallback": "target_autoregressive", "reason": "tokenizer_incompatible"}

    offline = lab.run_round(
        context=[10],
        draft=lambda *_: (_ for _ in ()).throw(TimeoutError()),
        verify=lambda *_: ([1], 1.0),
        compatibility=_compat(True),
        metrics=SpeculativeMetrics(),
    )
    assert offline["fallback"] == "target_autoregressive"
    assert "TimeoutError" in offline["reason"]


def test_installed_llama_help_local_only():
    result = inspect_llama_external_draft_support("--spec-draft-model FILE\n--spec-type draft-simple")
    assert result["local_draft_supported"] is True
    assert result["external_draft_supported"] is False


def test_circuit_breaker_opens_and_recovers():
    breaker = CircuitBreaker(failure_limit=2, cooldown_sec=10)
    breaker.failure(now=100)
    assert breaker.allow(now=101)
    breaker.failure(now=102)
    assert not breaker.allow(now=103)
    assert breaker.allow(now=113)
