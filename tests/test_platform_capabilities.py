import pytest

from ralfloop_agent.unified_assistant.platform import (
    CapabilityDescriptor, CapabilityPermission, CapabilityRegistry, PromotionState,
    RetrievalEvalCase, evaluate_retrieval,
)


def capability(identity: str, domain: str, keywords=(), *, permission=CapabilityPermission.READ, enabled=True, promotion=PromotionState.READ_ONLY, health="ok"):
    return CapabilityDescriptor(
        capability_id=identity, server_id=f"{domain}.mcp", domain=domain,
        name=identity, description=f"Semantic {identity}", keywords=keywords,
        permission=permission, source_system=domain, version="1",
        enabled=enabled, promotion=promotion, health=health,
        approval_required=permission in {CapabilityPermission.EXECUTE, CapabilityPermission.ADMIN},
        side_effect=permission in {CapabilityPermission.EXECUTE, CapabilityPermission.ADMIN},
    )


def test_registry_denies_unknown_and_duplicate_capabilities():
    row = capability("arci_verify_membership", "arci")
    with pytest.raises(ValueError, match="duplicate"):
        CapabilityRegistry((row, row))
    with pytest.raises(KeyError, match="denied"):
        CapabilityRegistry((row,)).get("generic_api_call")


def test_retrieval_routes_and_filters_permission_health_and_state():
    registry = CapabilityRegistry((
        capability("arci_verify_membership", "arci", ("socio", "tessera", "membership")),
        capability("jellyfin_get_user", "jellyfin", ("utente", "account", "accesso")),
        capability("jellyfin_delete_user", "jellyfin", ("utente",), permission=CapabilityPermission.EXECUTE),
        capability("memory_get_practice", "memory", ("pratica", "stato"), health="down"),
        capability("bandi_search", "bandi", ("bando", "scadenza"), enabled=False, promotion=PromotionState.DISABLED),
    ))
    ids = {row.capability_id for row in registry.retrieve("controlla socio tessera e accesso utente Jellyfin", limit=8)}
    assert ids == {"arci_verify_membership", "jellyfin_get_user"}


def test_privileged_capability_requires_approval_and_read_has_no_side_effect():
    base = dict(capability_id="x", server_id="x", domain="x", name="x", description="x", source_system="x", version="1", enabled=True, promotion="ACTIVE")
    with pytest.raises(ValueError, match="requires_approval"):
        CapabilityDescriptor(**base, permission="EXECUTE", side_effect=True)
    with pytest.raises(ValueError, match="read_capability"):
        CapabilityDescriptor(**base, permission="READ", side_effect=True)


def test_retrieval_eval_104_realistic_cases_beats_all_tools_baseline():
    rows = (
        ("arci_verify_membership", "arci", ("socio", "tessera", "membership")),
        ("jellyfin_get_user", "jellyfin", ("jellyfin", "utente", "account")),
        ("bandi_get_deadlines", "bandi", ("bandi", "scadono", "scadenze")),
        ("memory_get_practice", "memory", ("pratica", "punto", "stato")),
        ("memory_get_timeline", "memory", ("timeline", "cambiato", "cronologia")),
        ("memory_find_previous_cases", "memory", ("simile", "precedente", "gia")),
        ("tiremm_get_blocked", "tiremm_admin", ("bloccato", "bloccate", "blocker")),
        ("tiremm_get_next_actions", "tiremm_admin", ("prossima", "azione", "fare")),
        ("tiremm_get_sources", "tiremm_admin", ("fonte", "sai", "evidenza")),
        ("runtsuite_list_projects", "runtsuite", ("runtsuite", "progetti")),
        ("runtsuite_list_meetings", "runtsuite", ("runtsuite", "riunioni")),
        ("jellyfin_list_users", "jellyfin", ("jellyfin", "utenti", "elenco")),
        ("arci_list_cards", "arci", ("arci", "tessere", "elenco")),
    )
    registry = CapabilityRegistry(capability(identity, domain, keywords) for identity, domain, keywords in rows)
    prompts = (
        ("controlla socio membership", "arci_verify_membership"),
        ("trova account utente jellyfin", "jellyfin_get_user"),
        ("quali bandi scadono", "bandi_get_deadlines"),
        ("a che punto pratica", "memory_get_practice"),
        ("cosa e cambiato timeline", "memory_get_timeline"),
        ("caso simile precedente", "memory_find_previous_cases"),
        ("cosa risulta bloccato", "tiremm_get_blocked"),
        ("qual e prossima azione", "tiremm_get_next_actions"),
        ("da quale fonte lo sai", "tiremm_get_sources"),
        ("progetti runtsuite", "runtsuite_list_projects"),
        ("riunioni runtsuite", "runtsuite_list_meetings"),
        ("elenco utenti jellyfin", "jellyfin_list_users"),
        ("elenco tessere arci", "arci_list_cards"),
    )
    variants = ("", " per Tiremm", " adesso", " per favore", " stato corrente", " controllo", " amministrazione", " verifica")
    recalls = []
    selected_counts = []
    for prompt, expected in prompts:
        for suffix in variants:
            selected = registry.retrieve(prompt + suffix, limit=3)
            ids = {row.capability_id for row in selected}
            recalls.append(float(expected in ids))
            selected_counts.append(len(ids))
    assert len(recalls) == 104
    assert sum(recalls) / len(recalls) == 1.0
    assert max(selected_counts) <= 3
    assert sum(selected_counts) / len(selected_counts) < len(rows)


def test_retrieval_metrics_cover_required_dimensions():
    registry = CapabilityRegistry((
        capability("arci_verify_membership", "arci", ("socio", "tessera")),
        capability("jellyfin_get_user", "jellyfin", ("account", "utente")),
        capability("bandi_search", "bandi", ("bando", "contributo")),
    ))
    metrics = evaluate_retrieval(registry, (
        RetrievalEvalCase("arci", "verifica tessera socio", frozenset({"arci_verify_membership"})),
        RetrievalEvalCase("jellyfin", "trova account utente", frozenset({"jellyfin_get_user"})),
    ), k=2)
    assert metrics.cases == 2
    assert metrics.recall_at_k == metrics.task_success == 1.0
    assert 0 < metrics.precision_at_k <= 1
    assert metrics.schema_tokens_injected > 0
    assert metrics.mean_selected_count <= 2
    assert metrics.latency_mean_ms >= 0
    assert metrics.latency_p95_ms >= metrics.latency_mean_ms
    assert metrics.hallucinated_tools == 0
