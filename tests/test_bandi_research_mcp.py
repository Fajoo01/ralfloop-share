from __future__ import annotations

from pathlib import Path

from ralfloop_agent.domains.bandi_semantic_retrieval import discover_official_documents
from ralfloop_agent.domains.bandi_weekly_research import _tiremm_eligibility_fit
from ralfloop_agent.unified_assistant.bandi_research_mcp import BandiResearchMCPServer, TOOLS
from ralfloop_agent.unified_assistant.planner import UnifiedPlanner
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


def test_bandi_mcp_tool_allowlist_is_exact(tmp_path: Path) -> None:
    server = BandiResearchMCPServer(tmp_path)
    names = {row["name"] for row in server.list_tools()}
    assert names == TOOLS == {
        "bandi_research_now", "bandi_latest", "bandi_search_latest", "bandi_get_opportunity",
    }


def test_bandi_latest_without_report_is_read_only(tmp_path: Path) -> None:
    result = BandiResearchMCPServer(tmp_path).call("bandi_latest", {"limit": 5})
    payload = result["structuredContent"]
    assert payload["ok"] is True
    assert payload["status"] == "no_report"
    assert payload["writes"] == 0
    assert payload["sends"] == 0


def test_planner_routes_fresh_funding_search_to_bandi_research() -> None:
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    plan = planner.validate(planner.plan("Cercami bandi adatti a Tiremm"))
    assert plan.intent == "bandi.research"
    assert [row.skill for row in plan.assignments] == ["bandi.research"]


def test_planner_routes_specific_tiremm_review_to_eligibility() -> None:
    planner = UnifiedPlanner(UnifiedRegistryFacade())
    plan = planner.validate(planner.plan("Valuta il bando Regione Lombardia per Tiremm"))
    assert plan.intent == "bandi.eligibility"


def test_tiremm_fit_rejects_restricted_pro_loco_call() -> None:
    fit, reason = _tiremm_eligibility_fit([
        "Beneficiari: Pro Loco iscritte al RUNTS e all'Albo regionale delle pro loco."
    ])
    assert fit == "ineligible"
    assert reason == "pro_loco_only"


def test_tiremm_fit_accepts_explicit_aps_call() -> None:
    fit, reason = _tiremm_eligibility_fit([
        "Chi può partecipare: Associazioni di Promozione Sociale iscritte al RUNTS."
    ])
    assert fit == "compatible"
    assert reason == "aps_ets_explicit"


def test_generic_bandi_portal_link_is_not_treated_as_critical_call() -> None:
    html = '''<html><body>
      <a href="/servizi/servizio/bandi">Bandi e Servizi</a>
      <a href="/servizi/servizio/bandi/download/x?fileName=Avviso.pdf">Avviso ufficiale</a>
    </body></html>'''
    rows = discover_official_documents(
        html,
        "https://www.bandi.regione.lombardia.it/servizi/servizio/bandi/dettaglio/x",
    )
    assert [row.document_type for row in rows] == ["official_call_text"]
