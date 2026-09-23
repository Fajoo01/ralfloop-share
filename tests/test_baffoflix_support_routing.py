from ralfloop_agent.integration.interaction_router import classify_interaction
from ralfloop_agent.unified_assistant.capability_rag_router import CapabilityRAGRouter
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from ralfloop_agent.unified_assistant import safe_mcp_read_adapters as adapters


class NoModel:
    def health(self):
        return False


def planner() -> UnifiedPlanner:
    registry = UnifiedRegistryFacade()
    return UnifiedPlanner(
        registry,
        capability_router=CapabilityRAGRouter(registry, client=NoModel()),
    )


def test_natural_baffoflix_query_leaves_chat_only():
    decision = classify_interaction("qual è l'indirizzo di BaffoFlix?")
    assert decision.interaction_mode == "agent"
    assert decision.capability == "baffoflix.support"
    assert decision.approval_required is False


def test_assistant_v1_probe_routes_baffoflix_to_unified_mcp():
    from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags
    from ralfloop_agent.unified_assistant.runtime import unified_route_probe

    route = unified_route_probe(
        "qual è l'indirizzo di BaffoFlix?",
        {
            "source": "ralf_terminal",
            "assistant_surface": "assistant_v1",
            "session_id": "baffoflix-route-probe",
        },
        flags_override=AssistantFeatureFlags(unified_assistant=True),
    )

    assert route is not None
    assert route["intent"] == "baffoflix.support"
    assert route["skills_used"] == ["baffoflix.support"]
    assert route["mcp_connectors"] == ["jellyfin.identity.mcp.read"]
    assert route["task_mode"] == "tool_backed_read"
    assert route["requires_confirmation"] is False


def test_baffoflix_queries_route_to_read_support_skill():
    for query in (
        "qual è l'indirizzo di BaffoFlix?",
        "ho dimenticato la password di BaffoFlix",
        "come accedo a BaffoFlix?",
    ):
        plan = planner().validate(planner().plan(query))
        assert plan.intent == "baffoflix.support"
        assert plan.domains == ("jellyfin",)
        assert plan.assignments[0].policy.value == "READ"


def test_access_adapter_calls_only_public_access_tool(monkeypatch):
    calls = []

    def fake_call(provider, tool, arguments):
        calls.append((provider, tool, dict(arguments)))
        return {
            "ok": True,
            "status": "AVAILABLE",
            "landing_url": "https://example.org/b",
            "server_url": "http://public.example:8096",
            "public_health": {"server_name": "Baffoflix", "version": "10.11.6"},
        }

    monkeypatch.setattr(adapters, "_call", fake_call)
    assignment = planner().plan("qual è l'indirizzo di BaffoFlix?").assignments[0]
    artifact = adapters.baffoflix_support_adapter(assignment, {"user.goal": assignment.objective})

    assert artifact.status == "completed"
    assert calls == [("jellyfin", "baffoflix_get_access_info", {})]
    assert artifact.payload["support_intent"] == "access_info"
    assert artifact.payload["side_effects"] == 0
    assert "http://public.example:8096" in artifact.payload["message"]


def test_password_support_never_resets_or_authorizes(monkeypatch):
    calls = []

    def fake_call(provider, tool, arguments):
        calls.append((provider, tool, dict(arguments)))
        return {
            "ok": True,
            "status": "AVAILABLE",
            "landing_url": "https://example.org/b",
            "server_url": "http://public.example:8096",
            "public_health": {"server_name": "Baffoflix", "version": "10.11.6"},
        }

    monkeypatch.setattr(adapters, "_call", fake_call)
    assignment = planner().plan("ho dimenticato la password di BaffoFlix").assignments[0]
    artifact = adapters.baffoflix_support_adapter(assignment, {"user.goal": assignment.objective})

    assert artifact.status == "completed"
    assert calls == [("jellyfin", "baffoflix_get_access_info", {})]
    assert artifact.payload["support_intent"] == "password_recovery"
    assert artifact.payload["side_effects"] == 0
    assert artifact.payload["writes"] == 0
    assert "username esatto" in artifact.payload["message"]
    assert "Non modifico password" in artifact.payload["message"]
