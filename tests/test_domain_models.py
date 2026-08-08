from ralfloop_agent.domains.models import DomainManifest, DomainRule
from ralfloop_agent.domains.storage import now_iso


def test_domain_manifest_required_fields_serializable():
    m = DomainManifest(
        domain_id="x", display_name="X", description="d", version="1.0.0", state="draft",
        created_at=now_iso(), updated_at=now_iso(), created_by="test", scope=["s"], out_of_scope=["o"],
        source_requirements=["official_manual"], deterministic_capabilities=[], jury_capabilities=[],
        rule_precedence=[], minimum_source_count=1, minimum_test_pass_rate=1.0, content_hash="h", schema_version="1.0",
    )
    assert m.to_dict()["state"] == "draft"


def test_rule_requires_source_or_local_policy():
    r = DomainRule("r1", "desc", 1, {}, {}, source_refs=["s1"])
    assert r.to_dict()["source_refs"] == ["s1"]
