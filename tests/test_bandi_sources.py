from ralfloop_agent.unified_assistant.bandi_sources import (
    SOURCE_CATALOG, SourceMode, SourcePriority, enabled_sources,
)


def test_bandi_source_order_is_explicit_and_unverified_sources_fail_closed():
    assert SourcePriority.OFFICIAL_API < SourcePriority.FRONTEND_API < SourcePriority.OPEN_DATA
    assert SourcePriority.OPEN_DATA < SourcePriority.RSS < SourcePriority.STRUCTURED_HTML
    assert SourcePriority.STRUCTURED_HTML < SourcePriority.WEB_SEARCH < SourcePriority.BROWSER
    assert enabled_sources() == ()
    assert all(not row.enabled for row in SOURCE_CATALOG)


def test_priority_inventory_covers_tiremm_sources_without_framework_hardcoding():
    identities = {row.source_id for row in SOURCE_CATALOG}
    assert {
        "comune_milano", "regione_lombardia", "fondazione_cariplo",
        "csv_lombardia", "ministero_lavoro", "runts",
        "politiche_giovanili", "fondazioni_bancarie",
        "intesa_fondo_beneficenza",
    } <= identities
    assert any(row.mode is SourceMode.FRONTEND_API for row in SOURCE_CATALOG)
