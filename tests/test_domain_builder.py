from pathlib import Path

from ralfloop_agent.domains.builder import DomainBuilder
from ralfloop_agent.domains.registry import DomainRegistry


def test_domain_missing_creates_draft_with_source(tmp_path):
    source = tmp_path / "manual.md"
    source.write_text("official enough", encoding="utf-8")
    reg = DomainRegistry(tmp_path / "domains")
    result = DomainBuilder(reg).create_draft("Nuovo dominio operativo", [str(source)], {})
    assert result.status == "draft"
    assert result.approval_required is True
    assert Path(result.draft_path).exists()


def test_no_source_blocks_operational_rules(tmp_path):
    result = DomainBuilder(DomainRegistry(tmp_path / "domains")).create_draft("Dominio senza fonti", [], {})
    assert result.status == "invalid"
    assert "insufficient_sources" in result.blocking_issues
