from ralfloop_agent.domains.registry import DomainRegistry
from ralfloop_agent.domains.resolver import DomainResolver
from ralfloop_agent.domains.storage import write_text_atomic, write_yaml_atomic


def test_exact_domain_match():
    res = DomainResolver(DomainRegistry()).resolve("qualunque", {"domain_id": "arithmetic_basic"})
    assert res.status == "resolved"
    assert res.confidence == 1.0


def test_domain_missing():
    res = DomainResolver(DomainRegistry()).resolve("Valuta il rischio di un dominio mai registrato")
    assert res.status == "missing"


def test_domain_ambiguous(monkeypatch):
    resolver = DomainResolver(DomainRegistry())
    monkeypatch.setattr(resolver, "_candidates", lambda goal: [
        {"domain_id": "a", "version": "1", "confidence": 0.7, "reason_codes": ["scope_terms"]},
        {"domain_id": "b", "version": "1", "confidence": 0.68, "reason_codes": ["scope_terms"]},
    ])
    res = resolver.resolve("x")
    assert res.status == "ambiguous"
    assert res.requires_jury_for_domain_selection is True


def test_custom_active_domain_resolves_from_scope(tmp_path):
    reg = DomainRegistry(tmp_path / "domains")
    path = reg.root / "active" / "custom_ops" / "1.0.0"
    manifest = {
        "domain_id": "custom_ops",
        "display_name": "Custom Ops",
        "description": "custom",
        "version": "1.0.0",
        "state": "active",
        "created_at": "2026-07-11T00:00:00Z",
        "updated_at": "2026-07-11T00:00:00Z",
        "created_by": "test",
        "approved_by": "human",
        "approved_at": "2026-07-11T00:00:00Z",
        "languages": ["it"],
        "scope": ["backup operativo"],
        "out_of_scope": ["side effects"],
        "source_requirements": ["approved_local_policy"],
        "deterministic_capabilities": [],
        "jury_capabilities": [],
        "external_action_policy": "deny",
        "rule_precedence": [],
        "minimum_source_count": 1,
        "minimum_test_pass_rate": 1.0,
        "content_hash": "x",
        "schema_version": "1.0",
        "aliases": [],
        "jury_review_completed": True,
        "red_team_completed": True,
    }
    write_yaml_atomic(path / "domain.yaml", manifest)
    write_text_atomic(path / "sources.jsonl", "")
    reg.promote("custom_ops", "1.0.0", path)
    res = DomainResolver(reg).resolve("backup operativo urgente")
    assert res.status == "resolved"
    assert res.domain_id == "custom_ops"
