from ralfloop_agent.domains.reasoning_router import DomainReasoningRouter, RecursiveRoutingFlags


RESOLVED = {"status": "resolved", "domain_id": "synthetic", "version": "1"}


def route(classification, deterministic=None, **kwargs):
    router = DomainReasoningRouter(RecursiveRoutingFlags(frozenset({classification}), False))
    return router.select(domain_resolution=RESOLVED, classification=classification, deterministic_result=deterministic or {"complete": False}, **kwargs)


def test_recursive_selected_for_qualitative_judgment():
    assert route("qualitative_judgment")["selected_backend"] == "recursive_mas_native"


def test_recursive_selected_for_conflicting_sources():
    out = route("conflicting_sources", {"complete": False, "conflicts": [{"id": "c"}]})
    assert out["selected_backend"] == "recursive_mas_native"


def test_recursive_selected_for_strategic_assessment():
    assert route("strategic_assessment")["use_recursive"] is True


def test_recursive_not_selected_for_deterministic_task():
    out = route("deterministic", {"complete": True})
    assert out["selected_backend"] == "deterministic_engine"


def test_recursive_not_selected_for_lookup():
    out = route("lookup_complete", {"complete": False})
    assert out["selected_backend"] == "deterministic_engine"


def test_domain_required():
    out = DomainReasoningRouter().select(domain_resolution={"status": "missing"}, classification="strategic_assessment")
    assert out["selected_backend"] == "domain_creation_required"


def test_feature_flags_default_off(monkeypatch):
    for name in (
        "RALF_RECURSIVE_QUALITATIVE_JUDGMENT",
        "RALF_RECURSIVE_STRATEGIC_ASSESSMENT",
        "RALF_RECURSIVE_CONFLICTING_SOURCES",
        "RALF_RECURSIVE_EVIDENCE_SYNTHESIS",
        "RALF_RECURSIVE_DOMAIN_VALIDATION",
    ):
        monkeypatch.delenv(name, raising=False)
    out = DomainReasoningRouter().select(domain_resolution=RESOLVED, classification="strategic_assessment")
    assert out["selected_backend"] == "single_qwen_7b_with_domain"
