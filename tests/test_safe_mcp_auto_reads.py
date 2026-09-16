from __future__ import annotations

from ralfloop_agent.unified_assistant.capability_rag_router import CapabilityRAGRouter
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from ralfloop_agent.unified_assistant import safe_mcp_read_adapters as adapters


class NoModel:
    def health(self):
        return False


def _planner() -> UnifiedPlanner:
    registry = UnifiedRegistryFacade()
    return UnifiedPlanner(
        registry,
        capability_router=CapabilityRAGRouter(registry, client=NoModel()),
    )


def test_new_safe_reads_auto_route_to_logical_skills():
    planner = _planner()
    cases = {
        "pratica RUNTS bilancio 2025": ("runts.context", "runts"),
        "profilo ARCI del circolo": ("arci.context", "arci"),
        "film non identificati Jellyfin": ("jellyfin.identify", "jellyfin"),
        "insegnante spiegami le frazioni": ("education.tutor", "education"),
        "memoria operativa documenti": ("knowledge.retrieve", "knowledge"),
    }
    for query, (skill, domain) in cases.items():
        plan = planner.validate(planner.plan(query))
        assert plan.intent == skill
        assert plan.domains == (domain,)
        assert plan.assignments[0].skill == skill
        assert plan.assignments[0].policy.value == "READ"


def test_jellyfin_write_intent_is_never_downgraded_to_read():
    plan = _planner().validate(
        _planner().plan("applica identità film Jellyfin")
    )
    assert plan.intent == "jellyfin.apply_identity"
    assert plan.assignments[0].policy.value == "PROTECTED"


def test_arci_mutation_is_denied_not_read():
    plan = _planner().plan("modifica socio ARCI")
    assert plan.intent == "assistant.reject"
    assert plan.assignments[0].policy.value == "DENY"


def test_arci_member_query_does_not_auto_read_member_list(monkeypatch):
    called = []
    monkeypatch.setattr(adapters, "_call", lambda *args, **kwargs: called.append(args))
    assignment = _planner().plan("soci ARCI").assignments[0]
    artifact = adapters.arci_context_adapter(assignment, {"user.goal": "soci ARCI"})
    assert artifact.status == "clarification_required"
    assert called == []


def test_jellyfin_auto_read_calls_list_only(monkeypatch):
    calls = []
    def fake_call(provider, tool, arguments):
        calls.append((provider, tool, dict(arguments)))
        return [{"item_id": "a" * 32, "name": "Unknown Film", "year": 2001}]
    monkeypatch.setattr(adapters, "_call", fake_call)
    assignment = _planner().plan("film non identificati Jellyfin").assignments[0]
    artifact = adapters.jellyfin_identify_adapter(assignment, {"user.goal": assignment.objective})
    assert artifact.status == "completed"
    assert calls == [("jellyfin", "jellyfin_list_unidentified_movies", {})]


def test_teacher_without_session_never_calls_mcp(monkeypatch):
    calls = []
    monkeypatch.setattr(adapters, "_call", lambda *args, **kwargs: calls.append(args))
    assignment = _planner().plan("insegnante spiegami le frazioni").assignments[0]
    artifact = adapters.education_tutor_adapter(assignment, {"user.goal": assignment.objective})
    assert artifact.status == "clarification_required"
    assert calls == []
