from __future__ import annotations

from ralfloop_agent.integration.abc_relation_read import (
    ABCRelationReadAdapter,
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
)
from src.router import CapabilityRouter


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_state(self):
        self.calls.append("abc_get_state")
        return {"snapshot": "ok"}

    def analyze(self):
        self.calls.append("abc_analyze")
        return {"score": 42}

    def timeline(self, *, limit=100, kind=None):
        self.calls.append("abc_get_timeline")
        return [{"event_id": "evt"}]

    def references(self):
        self.calls.append("abc_get_reference_library")
        return [{"key": "dialogo_strategico"}]


class _LegacyTimelineClient(_FakeClient):
    def get_state(self):
        self.calls.append("abc_get_state")
        return {"snapshot": {"legacy_timeline": [{"event": "legacy event"}]}}

    def timeline(self, *, limit=100, kind=None):
        self.calls.append("abc_get_timeline")
        return []


class _BrokenClient:
    def get_state(self):
        raise OSError("socket unavailable")


def test_relational_query_routes_to_read_mcp_without_confirmation():
    route = CapabilityRouter().route("controlla la strategia relazionale")

    assert route.mode == "check_only"
    assert "abc_relation" in route.mcp_used
    assert route.requires_confirmation is False


def test_requested_runtime_phrases_route_to_abc():
    router = CapabilityRouter()
    for query in (
        "mostrami gli ultimi eventi",
        "analizza la situazione usando anche dialogo strategico e manuali di psicologia",
    ):
        route = router.route(query)
        assert route.mode == "check_only"
        assert "abc_relation" in route.mcp_used
        assert route.requires_confirmation is False


def test_unrelated_project_report_does_not_route_to_abc():
    route = CapabilityRouter().route("controlla la relazione annuale del progetto")

    assert "abc_relation" not in route.mcp_used


def test_external_mcp_confirmation_semantics_are_unchanged():
    route = CapabilityRouter().route("invia una email")

    assert route.mode == "external_action"
    assert "google_workspace.gmail" in route.mcp_used
    assert route.requires_confirmation is True


def test_read_adapter_never_exposes_write_tools():
    assert "abc_record_event" not in READ_ONLY_TOOLS
    assert "abc_create_snapshot" not in READ_ONLY_TOOLS
    assert WRITE_TOOLS.isdisjoint(READ_ONLY_TOOLS)


def test_read_adapter_uses_only_minimum_context_by_default():
    client = _FakeClient()
    result = ABCRelationReadAdapter(lambda: client).resolve("rsc analizza la situazione")

    assert result.available is True
    assert client.calls == ["abc_get_state", "abc_analyze"]
    assert set(result.context) == {"state", "analysis"}


def test_read_adapter_adds_timeline_and_manuals_only_when_requested():
    client = _FakeClient()
    result = ABCRelationReadAdapter(lambda: client).resolve(
        "rsc mostrami timeline e manuali di psicologia e dialogo strategico"
    )

    assert result.available is True
    assert client.calls == [
        "abc_get_state",
        "abc_analyze",
        "abc_get_timeline",
        "abc_get_reference_library",
    ]
    assert set(result.context) == {"state", "analysis", "timeline", "references"}


def test_read_adapter_uses_snapshot_legacy_timeline_when_event_store_is_empty():
    client = _LegacyTimelineClient()
    result = ABCRelationReadAdapter(lambda: client).resolve("rsc mostrami gli ultimi eventi")

    assert result.available is True
    assert result.context["timeline"] == [{"event": "legacy event"}]
    assert client.calls[:3] == ["abc_get_state", "abc_analyze", "abc_get_timeline"]


def test_read_adapter_falls_back_to_legacy_without_writes():
    result = ABCRelationReadAdapter(lambda: _BrokenClient()).resolve("rl:abc")

    assert result.available is False
    assert result.fallback_skills == ("abc_memory", "abc_relcalc")
    assert "socket unavailable" in (result.error or "")
