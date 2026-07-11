from pathlib import Path

from ralfloop_agent.domains.models import DomainManifest
from ralfloop_agent.domains.registry import DomainRegistry
from ralfloop_agent.domains.storage import now_iso, write_text_atomic, write_yaml_atomic


def _manifest(domain_id="d", version="1.0.0", state="draft"):
    return DomainManifest(domain_id, domain_id, "desc", version, state, now_iso(), now_iso(), "test", scope=["scope"], out_of_scope=["out"], source_requirements=["approved_local_policy"], deterministic_capabilities=[], jury_capabilities=[], rule_precedence=[], minimum_source_count=1, minimum_test_pass_rate=1.0, content_hash="", schema_version="1.0")


def test_register_and_find_draft(tmp_path):
    reg = DomainRegistry(tmp_path / "domains")
    path = reg.root / "drafts" / "d" / "1.0.0"
    path.mkdir(parents=True)
    write_yaml_atomic(path / "domain.yaml", _manifest().to_dict())
    reg.register_draft(_manifest(), path)
    assert reg.get_domain("d", "1.0.0")["manifest"]["state"] == "draft"


def test_builtin_fixture_active_domain():
    reg = DomainRegistry(Path("/tmp/no-such-domain-root"))
    dom = reg.find_active("arithmetic_basic")
    assert dom["manifest"]["state"] == "active"
    assert dom["fixture"] is True


def test_checksum_invalid_quarantine(tmp_path):
    reg = DomainRegistry(tmp_path / "domains")
    path = reg.root / "active" / "d" / "1.0.0"
    path.mkdir(parents=True)
    data = _manifest(state="active").to_dict()
    write_yaml_atomic(path / "domain.yaml", data)
    write_text_atomic(path / "manifest.sha256", "wrong\n")
    reg.promote("d", "1.0.0", path)
    assert reg.verify_integrity("d", "1.0.0")["ok"] is False
