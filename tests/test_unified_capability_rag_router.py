from __future__ import annotations

from ralfloop_agent.local_arch.contracts import CompactRoute
from ralfloop_agent.unified_assistant.capability_rag_router import (
    CapabilityRAGIndex,
    CapabilityRAGRouter,
)
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade


def _first(query: str) -> str:
    rows = CapabilityRAGIndex(UnifiedRegistryFacade()).retrieve(query)
    assert rows, query
    return rows[0].skill_id


def test_capability_rag_routes_named_atm_without_regex_gap():
    assert _first("portami a casa") == "atm.route"
    assert _first("portami da md a casa") == "atm.route"
    assert _first("portami da casa a Sonia") == "atm.route"
    assert _first("come vado da md a casa") == "atm.route"


def test_capability_rag_routes_current_leaf_reads():
    cases = {
        "pioverà stasera?": "meteo.read",
        "che tempo fa a Milano": "meteo.read",
        "cerca la mail di ARCI": "email.search",
        "cerca i messaggi WhatsApp di Marco": "whatsapp.read",
        "leggi le campagne Mailchimp": "mailchimp.read",
        "quanto paghiamo sul portale Fastweb?": "fastweb.portal.read",
        "qual è lo stato della luce in Home Assistant?": "home.read",
    }

    for query, expected in cases.items():
        assert _first(query) == expected, query


def test_gmail_read_binds_least_privilege_provider():
    rows = CapabilityRAGIndex(UnifiedRegistryFacade()).retrieve(
        "cerca la mail di ARCI"
    )

    email = next(row for row in rows if row.skill_id == "email.search")

    assert any(
        "google_workspace.gmail.read_only" in provider
        for provider in email.providers
    )


def test_rag_returns_logical_skills_not_raw_mcp_provider_as_target():
    rows = CapabilityRAGIndex(UnifiedRegistryFacade()).retrieve(
        "portami da md a casa"
    )

    assert rows[0].skill_id == "atm.route"
    assert rows[0].skill_id != "atm.route.mcp"
    assert any("atm.route.mcp" in item for item in rows[0].providers)


class FakeFunctionGemma:
    def health(self):
        return True

    def classify(self, text, catalog, registry):
        names = {row["t"] for row in catalog}

        # Verifica fondamentale: FunctionGemma vede skill logiche.
        assert "atm.route.mcp" not in names

        if "portami" in text:
            assert "atm.route" in names
            return CompactRoute(1, "ET", "atm.route", c=1.0)

        return CompactRoute(1, "ET", sorted(names)[0], c=1.0)


def test_functiongemma_reranks_retrieved_logical_capabilities_only():
    router = CapabilityRAGRouter(
        UnifiedRegistryFacade(),
        client=FakeFunctionGemma(),
    )

    result = router.route("cerca messaggi posta")

    assert result is not None
    assert result["skill"] == "email.search"
    assert result["source"] == "functiongemma"
    assert result["confidence"] == 1.0


def test_unknown_general_chat_is_not_forced_into_mcp():
    rows = CapabilityRAGIndex(UnifiedRegistryFacade()).retrieve(
        "raccontami una barzelletta sui pinguini"
    )
    assert rows == ()


def test_generic_queries_do_not_select_a_leaf_capability():
    index = CapabilityRAGIndex(UnifiedRegistryFacade())
    for query in ("cerca informazioni", "fammi una ricerca", "controlla questa cosa", "dimmi qualcosa", "aiutami", "vediamo"):
        assert index.retrieve(query) == (), query


def test_foreign_query_is_none():
    assert CapabilityRAGIndex(UnifiedRegistryFacade()).retrieve("barzelletta sui pinguini") == ()
