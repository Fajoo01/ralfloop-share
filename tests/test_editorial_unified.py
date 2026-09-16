from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.editorial_mcp_adapter import EditorialMCPContext
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


def _brief(title, sections):
    return {"title": title, "sections": [{"title": item, "text": ""} for item in sections]}


def test_planner_routes_flyers_to_editorial_mcp():
    registry = UnifiedRegistryFacade()
    planner = UnifiedPlanner(registry)
    plan = planner.plan("valuta il volantino della ciclofficina")
    assert plan.intent == "editorial.flyer"
    assert plan.domains == ("editorial",)
    assert plan.assignments[0].skill == "editorial.flyer"
    assert plan.assignments[0].policy is PolicyClass.AUTO_WRITE


def test_registry_exposes_live_editorial_capabilities_when_socket_exists():
    tool = next(x for x in UnifiedRegistryFacade().list_tools() if x.id == "editorial.flyer.mcp")
    assert "flyer_marketing_review" in tool.capabilities
    assert "flyer_media_review" in tool.capabilities
    assert tool.classification is PolicyClass.AUTO_WRITE


class FakeEditorial(EditorialMCPContext):
    def __init__(self):
        pass

    def payload(self, name, arguments):
        if name == "flyer_projects":
            return ["tiremm-adulti-2026", "tiremm-ciclofficina-2026", "tiremm-festa-tesseramento-2026"]
        project = arguments["project"]
        if name == "flyer_brief":
            if project == "tiremm-adulti-2026":
                return _brief("Attività per adulti", ["Corsi di ciclomeccanica — Fabio", "Cucito"])
            if project == "tiremm-ciclofficina-2026":
                return _brief("Impara a curare la tua bici", ["Ciclomeccanica — Fabio"])
            return _brief("Festa di tesseramento", ["Tesseramento"])
        if name == "flyer_review":
            return {"ok": True, "warnings": []}
        if name == "flyer_marketing_review":
            return {"ok": True, "score": 86, "grade": "B", "warnings": []}
        if name == "flyer_media_review":
            return {"ok": True, "warnings": []}
        if name == "flyer_render":
            return {"print_ready": True, "output": "/tmp/render"}
        raise AssertionError(name)


def test_editorial_dispatch_resolves_ciclofficina_to_ciclomeccanica_project():
    result = FakeEditorial().request("valuta il volantino della ciclofficina")
    assert result["project"] == "tiremm-ciclofficina-2026"
    assert result["operation"] == "full_review"
    assert "86/100" in result["message"]


def test_editorial_dispatch_keeps_catalogue_request_on_adult_project():
    result = FakeEditorial().request("valuta il volantino attività per adulti")
    assert result["project"] == "tiremm-adulti-2026"
    assert result["operation"] == "full_review"


def test_editorial_dispatch_renders_named_project():
    result = FakeEditorial().request("renderizza il flyer tiremm-adulti-2026")
    assert result["status"] == "completed"
    assert result["operation"] == "render"
    assert result["evidence_refs"] == ["/tmp/render"]
