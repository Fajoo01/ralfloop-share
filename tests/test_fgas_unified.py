from __future__ import annotations

from ralfloop_agent.unified_assistant.contracts import PolicyClass
from ralfloop_agent.unified_assistant.fgas_mcp_adapter import FGasMCPContext
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


def test_planner_routes_fgas_whatsapp_generation_to_local_mcp():
    registry = UnifiedRegistryFacade()
    planner = UnifiedPlanner(registry)
    plan = planner.plan("prepara il modulo F-Gas dai messaggi WhatsApp")
    assert plan.intent == "fgas.installation"
    assert plan.domains == ("fgas",)
    assert plan.assignments[0].skill == "fgas.installation"
    assert plan.assignments[0].policy is PolicyClass.AUTO_WRITE


def test_explicit_email_send_remains_confirmation_gated():
    registry = UnifiedRegistryFacade()
    planner = UnifiedPlanner(registry)
    plan = planner.plan("manda una email a Morgan con il modulo F-Gas")
    assert plan.intent == "email.compose"
    assert plan.assignments[0].policy is PolicyClass.CONFIRM_WRITE


def test_registry_exposes_fgas_as_local_artifact_only():
    tool = next(x for x in UnifiedRegistryFacade().list_tools() if x.id == "fgas.installation.mcp")
    assert "fgas_prepare_from_whatsapp" in tool.capabilities
    assert tool.classification is PolicyClass.AUTO_WRITE
    assert tool.side_effect_class == "local_artifact_generation_only"
    assert "external_sends=0" in tool.verification_method


class FakeFGas(FGasMCPContext):
    def __init__(self):
        pass

    def call(self, name, arguments):
        assert name == "fgas_prepare_from_whatsapp"
        assert arguments == {}
        return {
            "ok": True,
            "complete": True,
            "status": "COMPLETE",
            "missing": [],
            "render": {
                "artifacts": {
                    "docx": "/var/lib/ralf-fgas/output/example.docx",
                    "pdf": "/var/lib/ralf-fgas/output/example.pdf",
                }
            },
            "external_sends": 0,
            "external_writes": 0,
        }


def test_fgas_adapter_never_sends_even_when_user_asks_to_send():
    result = FakeFGas().request("prepara e invia via email il modulo F-Gas")
    assert result["send_requested"] is True
    assert result["external_sends"] == 0
    assert result["external_writes"] == 0
    assert result["approval_required_for_external_send"] is True
    assert result["artifact_paths"][-1].endswith(".pdf")
    assert "approvazione" in result["message"].casefold()
