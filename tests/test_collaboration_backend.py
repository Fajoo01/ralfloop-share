from __future__ import annotations

from ralfloop_agent.integration.collaboration_backend import select_collaboration_backend
from src.models import JuryPolicy, LLMJudgeVerdict
from src.router import route_task
from src.text_mas_proxy import build_text_mas_trace


def test_native_disabled_by_default_selects_text_proxy(monkeypatch):
    monkeypatch.delenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", raising=False)
    policy = JuryPolicy(enabled=True, mode="required", triggers=["explicit_jury"])

    backend = select_collaboration_backend(policy, style="sequential")

    assert backend.selected_backend == "text_proxy"
    assert backend.implementation_level == "text_proxy"
    assert backend.native_latent is False
    assert backend.fallback_used is False


def test_route_only_does_not_probe_or_load_native(monkeypatch):
    monkeypatch.setenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "1")
    monkeypatch.setenv("RALFLOOP_ALLOW_TEXT_MAS_FALLBACK", "1")
    policy = JuryPolicy(enabled=True, mode="required", triggers=["explicit_jury"])

    backend = select_collaboration_backend(policy, style="sequential", route_only=True)

    assert backend.selected_backend == "text_proxy"
    assert backend.fallback_used is True
    assert backend.fallback_reason == "route_only_does_not_probe_or_load_native_backend"


def test_fallback_text_proxy_is_explicit_when_native_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "1")
    monkeypatch.setenv("RALFLOOP_ALLOW_TEXT_MAS_FALLBACK", "1")
    monkeypatch.setenv("RALFLOOP_RECURSIVE_MAS_ROOT", str(tmp_path / "missing"))
    policy = JuryPolicy(enabled=True, mode="required", triggers=["explicit_jury"])

    backend = select_collaboration_backend(policy, style="sequential", route_only=False)

    assert backend.selected_backend == "text_proxy"
    assert backend.fallback_used is True
    assert "repository_missing" in (backend.fallback_reason or "")


def test_no_fallback_returns_unavailable(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", "1")
    monkeypatch.setenv("RALFLOOP_ALLOW_TEXT_MAS_FALLBACK", "0")
    monkeypatch.setenv("RALFLOOP_RECURSIVE_MAS_ROOT", str(tmp_path / "missing"))
    policy = JuryPolicy(enabled=True, mode="required", triggers=["explicit_jury"])

    backend = select_collaboration_backend(policy, style="sequential", route_only=False)

    assert backend.available is False
    assert backend.selected_backend == "unavailable"
    assert backend.native_latent is False


def test_single_backend_when_jury_not_required():
    backend = select_collaboration_backend(JuryPolicy(enabled=False), style="single")

    assert backend.selected_backend == "single"
    assert backend.implementation_level == "routing_only"


def test_trace_is_not_native_recursive_mas():
    route = route_task("telepatia giuria multiagent")

    trace = build_text_mas_trace("telepatia giuria multiagent", route)

    assert trace is not None
    assert trace["backend_name"] == "text_mas_proxy"
    assert trace["implementation_level"] == "text_proxy"
    assert trace["native_latent"] is False
    assert trace["trace_is_native_recursive_mas"] is False


def test_jury_policy_collaboration_backend_and_verification_are_distinct():
    route = route_task("invia telegram con risultato finale")

    assert route.jury_policy.enabled is True
    assert route.collaboration_backend.implementation_level == "text_proxy"
    assert route.verification_policy.verifier_type == "combined"
    assert route.verification_policy.judge_provider is None
    assert route.requires_confirmation is True


def test_llm_judge_is_not_jury():
    route = route_task("telepatia giuria multiagent")

    assert route.jury_policy.enabled is True
    assert route.verification_policy.verifier_type != "llm_judge"
    assert route.verification_policy.judge_provider is None


def test_llm_judge_contract_is_separate_from_jury():
    verdict = LLMJudgeVerdict(
        candidate_answer="ok",
        user_goal="valuta risposta",
        criteria=["qualitative_consistency"],
        **{"pass": True},
        score=0.8,
        reason="meets criterion",
        judge_provider="test",
    )

    payload = verdict.model_dump(by_alias=True)

    assert payload["pass"] is True
    assert payload["judge_provider"] == "test"
