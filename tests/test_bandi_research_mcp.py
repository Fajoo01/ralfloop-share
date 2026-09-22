from __future__ import annotations

import json
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


def test_search_latest_matches_exact_regione_reference(tmp_path: Path) -> None:
    state = tmp_path / "state"
    report = state / "runs" / "r1" / "report.json"
    report.parent.mkdir(parents=True)
    report.write_text(json.dumps({
        "status": "completed", "finished_at": "2026-09-16T12:00:00+00:00",
        "opportunities": [{
            "call_key": "abc12345", "canonical_call_id": "terzo-settore",
            "title": "Terzo settore Triennio 2026-2028", "funding_body": "Regione Lombardia",
            "primary_url": "https://www.bandi.regione.lombardia.it/x/RLJ12026054688",
            "score": 87, "priority": "HIGH", "status": "open", "deadline": "2026-10-16",
        }],
    }), encoding="utf-8")
    (state / "status.json").write_text(json.dumps({"last_report_json": str(report)}), encoding="utf-8")
    result = BandiResearchMCPServer(state).call(
        "bandi_search_latest", {"query": "RLJ12026054688", "limit": 5}
    )["structuredContent"]
    assert result["items"][0]["title"] == "Terzo settore Triennio 2026-2028"


def test_bandi_adapter_cercami_triggers_fresh_research_and_filters_irrelevant() -> None:
    from ralfloop_agent.unified_assistant.bandi_mcp_adapter import BandiMCPContext

    class FakeBandi(BandiMCPContext):
        def __init__(self) -> None:
            self.calls = []

        def call(self, name, arguments):
            self.calls.append((name, dict(arguments)))
            if name == "bandi_research_now":
                return {
                    "ok": True,
                    "status": "completed",
                    "items": [
                        {"call_key": "good0001", "title": "ETS", "score": 87, "tiremm_compatibility": "compatible"},
                        {"call_key": "maybe001", "title": "Da verificare", "score": 79, "tiremm_compatibility": "conditional_missing_primary_documents"},
                        {"call_key": "nutria01", "title": "Nutria", "score": 63, "tiremm_compatibility": "needs_review"},
                        {"call_key": "proloco1", "title": "Pro Loco", "score": 20, "tiremm_compatibility": "ineligible"},
                    ],
                    "writes": 0,
                    "sends": 0,
                }
            raise AssertionError(name)

    ctx = FakeBandi()
    result = ctx.request("Cercami bandi adatti a Tiremm")
    assert ctx.calls[0][0] == "bandi_research_now"
    assert result["operation"] == "fresh_research"
    assert [row["title"] for row in result["items"]] == ["ETS", "Da verificare"]
    assert result["raw_item_count"] == 4
    assert result["excluded_irrelevant_count"] == 2


def test_bandi_adapter_specific_ineligible_reference_is_not_hidden() -> None:
    from ralfloop_agent.unified_assistant.bandi_mcp_adapter import BandiMCPContext

    class FakeBandi(BandiMCPContext):
        def __init__(self) -> None:
            self.calls = []

        def call(self, name, arguments):
            self.calls.append((name, dict(arguments)))
            if name == "bandi_search_latest":
                return {"ok": True, "status": "completed", "items": [{
                    "call_key": "proloco1", "title": "Pro Loco", "score": 20,
                    "tiremm_compatibility": "ineligible", "primary_url": "https://example.invalid/RLJ12026099999",
                }], "writes": 0, "sends": 0}
            return {"ok": True, "status": "completed", "opportunity": {"title": "Pro Loco"}, "writes": 0, "sends": 0}

    result = FakeBandi().request("Valuta RLJ12026099999 per Tiremm")
    assert result["operation"] == "reference_lookup"
    assert result["items"][0]["tiremm_compatibility"] == "ineligible"
