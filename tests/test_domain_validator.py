from pathlib import Path

from ralfloop_agent.domains.builder import DomainBuilder
from ralfloop_agent.domains.registry import DomainRegistry, fixture_domain
from ralfloop_agent.domains.validator import DomainValidator


def test_rule_without_provenance_invalid():
    domain = fixture_domain("arithmetic_basic")
    domain["rules"][0] = {**domain["rules"][0], "source_refs": [], "rule_type": "source_derived"}
    result = DomainValidator().validate(domain)
    assert result.valid is False
    assert any("rule_without_provenance" in x for x in result.blocking_issues)


def test_source_not_verified_blocks(tmp_path):
    source = tmp_path / "manual.md"; source.write_text("x", encoding="utf-8")
    reg = DomainRegistry(tmp_path / "domains")
    draft = DomainBuilder(reg).create_draft("Dominio fonte", [str(source)], {})
    domain = reg.get_domain(draft.domain_id, draft.version)
    domain["sources"][0]["checksum"] = "bad"
    result = DomainValidator().validate(domain)
    assert result.valid is False
    assert any("source_checksum_invalid" in x for x in result.blocking_issues)


def test_blocking_conflict_prevents_validation(tmp_path):
    result = DomainBuilder(DomainRegistry(tmp_path / "domains")).create_draft("Dominio senza fonti", [], {})
    domain = {"manifest": {"domain_id": "d", "version": "0", "state": "draft", "scope": ["s"], "out_of_scope": ["o"], "minimum_source_count": 1, "minimum_test_pass_rate": 1.0}, "sources": [], "rules": [], "conflicts": [{"blocking": True}], "tests": [], "examples": []}
    validated = DomainValidator().validate(domain)
    assert validated.valid is False
    assert "unresolved_blocking_conflicts" in validated.blocking_issues


def test_tests_failed_prevent_validation():
    domain = fixture_domain("arithmetic_basic")
    domain["tests"][0]["expected"] = "6"
    result = DomainValidator().validate(domain)
    assert result.valid is False
    assert "minimum_test_pass_rate_not_met" in result.blocking_issues


def test_examples_and_tests_are_required():
    domain = fixture_domain("arithmetic_basic")
    domain["examples"] = []
    domain["tests"] = []
    result = DomainValidator().validate(domain)
    assert result.valid is False
    assert "examples_present" in result.blocking_issues
    assert "tests_present" in result.blocking_issues


def test_llm_generated_source_cannot_support_rule():
    domain = fixture_domain("arithmetic_basic")
    domain["sources"][0] = {**domain["sources"][0], "source_type": "LLM_generated_text"}
    domain["rules"][0] = {**domain["rules"][0], "rule_type": "source_derived"}
    result = DomainValidator().validate(domain)
    assert result.valid is False
    assert any("rule_source_insufficient" in item for item in result.blocking_issues)
