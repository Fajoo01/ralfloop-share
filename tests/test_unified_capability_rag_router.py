from __future__ import annotations

from ralfloop_agent.local_arch.contracts import CompactRoute
from ralfloop_agent.unified_assistant.capability_rag_router import (
    CapabilityRAGIndex,
    CapabilityRAGRouter,
)
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags
from ralfloop_agent.unified_assistant.pec_mcp_adapter import PecMCPContext
from ralfloop_agent.unified_assistant.runtime import _is_pec_runts_request, unified_route_probe


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
        "leggi la PEC del Difensore regionale": "pec.read",
        "posta certificata sulla TARI": "pec.read",
        "qual è lo stato della luce in Home Assistant?": "home.read",
        "quali bandi aperti abbiamo?": "bandi.research",
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

    result = router.route("messaggi posta")

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


def test_pec_rag_precedes_generic_home_verbs_and_gmail_post_word():
    router = CapabilityRAGRouter(UnifiedRegistryFacade())
    for query in (
        "Invia la PEC al Difensore regionale e porta a termine la pratica TARI",
        "controlla la posta certificata del Difensore regionale",
    ):
        result = router.route(query)
        assert result is not None, query
        assert result["skill"] == "pec.read", (query, result)
        assert result["domain"] == "pec", (query, result)


def test_capability_discovery_sees_full_mcp_catalog_without_authorizing_it():
    index = CapabilityRAGIndex(UnifiedRegistryFacade())
    cases = {
        "soci ARCI e tessere": "arci.context",
        "pratica RUNTS Lombardia": "runts.context",
        "insegnante quiz esercizi": "education.tutor",
        "identifica film Jellyfin": "jellyfin.identify",
        "memory documents search": "knowledge.retrieve",
        "bandi disponibili per APS": "bandi.research",
    }
    for query, expected in cases.items():
        rows = index.discover(query)
        assert rows, query
        assert rows[0].skill_id == expected, (query, rows)


def test_capability_discovery_can_describe_protected_mcp_but_route_cannot_select_it():
    index = CapabilityRAGIndex(UnifiedRegistryFacade())
    discovered = index.discover("applica identità film Jellyfin")
    protected = next(row for row in discovered if row.skill_id == "jellyfin.apply_identity")
    assert protected.policy.value == "PROTECTED"
    assert any("jellyfin.identity.mcp.write" in provider for provider in protected.providers)
    assert all(row.skill_id != "jellyfin.apply_identity" for row in index.retrieve("applica identità film Jellyfin"))


class FakeStandalonePecContext(PecMCPContext):
    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        if name == "pec_search_messages":
            return {
                "ok": True,
                "messages": [{
                    "native_id": "imap.42",
                    "sender": "difensore.regionale@pec.consiglio.regione.lombardia.it",
                    "subject": "FAGIOLI FABIO - RICHIESTA DI ADEMPIMENTI PRELIMINARI",
                    "received_at": "2026-09-01T10:00:00Z",
                    "body": "anteprima",
                    "attachments": [],
                    "source": {"locator": "imaps://example/INBOX?uid=42"},
                }],
                "writes": 0,
                "sends": 0,
            }
        if name == "pec_get_message":
            return {
                "ok": True,
                "message": {
                    "native_id": "imap.42",
                    "sender": "difensore.regionale@pec.consiglio.regione.lombardia.it",
                    "subject": "FAGIOLI FABIO - RICHIESTA DI ADEMPIMENTI PRELIMINARI",
                    "received_at": "2026-09-01T10:00:00Z",
                    "body": "Trasmettere la documentazione TARI richiesta.",
                    "attachments": [{"attachment_id": "a1", "filename": "richiesta.pdf"}],
                    "source": {"locator": "imaps://example/INBOX?uid=42"},
                },
                "writes": 0,
                "sends": 0,
            }
        raise AssertionError(name)


def _assistant_flags():
    return AssistantFeatureFlags(unified_assistant=True)


def test_generic_pec_uses_standalone_capability_not_legacy_runts_vertical():
    text = "Leggi la PEC del Difensore regionale sulla TARI"
    assert _is_pec_runts_request(text) is False
    route = unified_route_probe(
        text,
        {"source": "ralf_terminal"},
        flags_override=_assistant_flags(),
    )
    assert route is not None
    assert route["intent"] == "pec.read"
    assert route["domains"] == ["pec"]
    assert route["skills_used"] == ["pec.read"]
    assert route["mcp_used"] == ["pec.read.mcp"]
    assert route["write_policy"] == "no_write"


def test_specific_pec_search_reads_exact_message_and_never_writes():
    ctx = FakeStandalonePecContext()
    result = ctx.request("Leggi la PEC del Difensore regionale sulla TARI")
    assert ctx.calls == [
        ("pec_search_messages", {"query": "difensore", "limit": 100}),
        ("pec_get_message", {"message_id": "imap.42"}),
    ]
    assert result["operation"] == "search_and_read"
    assert "Trasmettere la documentazione TARI richiesta" in result["message"]
    assert result["writes"] == 0
    assert result["sends"] == 0


def test_send_request_routes_to_separate_confirmation_bound_writer():
    text = "Invia la PEC al Difensore regionale e porta a termine la pratica TARI"
    assert _is_pec_runts_request(text) is False
    route = unified_route_probe(
        text,
        {"source": "ralf_terminal"},
        flags_override=_assistant_flags(),
    )
    assert route is not None
    assert route["intent"] == "pec.prepare_send"
    assert route["domains"] == ["pec"]
    assert route["write_policy"] == "policy_gated"
    assert route["requires_confirmation"] is True
    assert "pec.write.mcp" in route["mcp_connectors"]
    assert "home" not in route["domains"]

    # The reader remains independently read-only even when handed a write-like sentence.
    result = FakeStandalonePecContext().request(text)
    assert result["write_requested"] is True
    assert result["writer_available"] is False
    assert result["approval_required_for_write"] is True
    assert "Nessuna PEC è stata inviata" in result["message"]
    assert result["writes"] == 0
    assert result["sends"] == 0
