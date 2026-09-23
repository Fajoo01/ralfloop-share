from __future__ import annotations

from datetime import date
from decimal import Decimal

from ralfloop_agent.unified_assistant.accounting import (
    accounting_read_adapter,
    ets_reporting_regime,
    statutory_runts_due_date,
    summarize_movements,
)
from ralfloop_agent.unified_assistant.capability_rag_router import CapabilityRAGIndex
from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags, PlanAssignment, PolicyClass
from ralfloop_agent.unified_assistant.registry import UnifiedRegistryFacade
from ralfloop_agent.unified_assistant.runtime import unified_route_probe


def test_accounting_domain_and_provider_are_registered():
    registry = UnifiedRegistryFacade()
    assert registry.domain("commercialista").id == "accounting"
    skill = registry.skill("accounting.read")
    assert skill.classification is PolicyClass.READ
    providers = [tool for tool in registry.list_tools() if "accounting.inspect" in tool.capabilities]
    assert providers
    assert providers[0].id == "accounting.local.read"
    assert providers[0].classification is PolicyClass.READ


def test_capability_rag_routes_commercialista_and_ets_accounting():
    index = CapabilityRAGIndex(UnifiedRegistryFacade())
    for query in (
        "fammi il commercialista",
        "controlla la contabilità di Tiremm",
        "preparami il quadro del rendiconto ETS",
        "riconcilia la prima nota",
    ):
        rows = index.retrieve(query)
        assert rows, query
        assert rows[0].skill_id == "accounting.read", (query, rows)


def test_unified_route_probe_exposes_read_only_accounting_connector():
    route = unified_route_probe(
        "fammi il commercialista e controlla la contabilità",
        {"source": "ralf_terminal"},
        flags_override=AssistantFeatureFlags(unified_assistant=True),
    )
    assert route is not None
    assert route["intent"] == "accounting.read"
    assert route["domains"] == ["accounting"]
    assert route["write_policy"] == "no_write"
    assert route["requires_confirmation"] is False
    assert route["mcp_connectors"] == ["accounting.local.read"]


def test_model_e_for_low_entry_ets_in_calendar_2026():
    result = ets_reporting_regime(
        annual_entries_eur="50.000,00",
        legal_personality=True,
        financial_year_end=date(2026, 12, 31),
        mainly_commercial=False,
        social_enterprise=False,
    )
    assert result["status"] == "determined"
    assert result["mode"] == "E"
    assert result["deadline_requires_live_check"] is True


def test_model_d_for_non_personified_ets_within_300k():
    result = ets_reporting_regime(
        annual_entries_eur=Decimal("100000"),
        legal_personality=False,
        financial_year_end=date(2026, 12, 31),
        mainly_commercial=False,
        social_enterprise=False,
    )
    assert result["status"] == "determined"
    assert result["mode"] == "D"


def test_competence_when_cash_threshold_is_exceeded():
    result = ets_reporting_regime(
        annual_entries_eur="350000",
        legal_personality=False,
        financial_year_end=date(2026, 12, 31),
        mainly_commercial=False,
        social_enterprise=False,
    )
    assert result["status"] == "determined"
    assert result["mode"] == "competence"


def test_missing_legal_tax_facts_never_get_invented():
    result = ets_reporting_regime(
        annual_entries_eur="50000",
        legal_personality=None,
        financial_year_end=date(2026, 12, 31),
        mainly_commercial=None,
        social_enterprise=None,
    )
    assert result["status"] == "requires_verified_inputs"
    assert set(result["missing"]) >= {
        "legal_personality", "mainly_commercial", "social_enterprise"
    }
    assert result["mode"] is None


def test_social_enterprise_or_mainly_commercial_is_not_forced_into_d_or_e():
    result = ets_reporting_regime(
        annual_entries_eur="50000",
        legal_personality=False,
        financial_year_end=date(2026, 12, 31),
        mainly_commercial=False,
        social_enterprise=True,
    )
    assert result["status"] == "special_rules_required"
    assert result["mode"] == "special_rules"


def test_statutory_runts_window_is_180_days_and_marked_for_live_check():
    assert statutory_runts_due_date(date(2026, 12, 31)) == date(2027, 6, 29)


def test_reconciliation_uses_decimal_and_preserves_evidence_refs():
    result = summarize_movements([
        {"direction": "entrata", "amount": "100,10", "evidence_ref": "doc:a"},
        {"direction": "uscita", "amount": "30,05", "evidence_ref": "doc:b"},
        {"direction": "?", "amount": "10", "evidence_ref": "doc:c"},
    ])
    assert result["incoming_eur"] == "100.10"
    assert result["outgoing_eur"] == "30.05"
    assert result["net_cash_eur"] == "70.05"
    assert result["unresolved_count"] == 1
    assert result["evidence_refs"] == ["doc:a", "doc:b"]


def test_adapter_never_claims_filing_payment_or_send():
    assignment = PlanAssignment(
        task_id="task.accounting",
        domain="accounting",
        skill="accounting.read",
        objective=(
            "Rendiconto 2026: entrate 50.000 euro, senza personalità giuridica, "
            "non impresa sociale, non principalmente commerciale"
        ),
        input_refs=("user.goal",),
        output_ref="artifact.accounting",
        policy=PolicyClass.READ,
    )
    artifact = accounting_read_adapter(assignment, {})
    assert artifact.payload["writes"] == 0
    assert artifact.payload["sends"] == 0
    assert artifact.payload["payments"] == 0
    assert artifact.payload["filings"] == 0
    assert artifact.payload["regime"]["mode"] == "E"
    assert artifact.payload["regime"]["annual_entries_eur"] == "50000"
