from __future__ import annotations

from pathlib import Path

import yaml

from ralfloop_agent.domains.existing_capability_importer import ExistingCapabilityImporter
from ralfloop_agent.domains.registry import DomainRegistry


def test_importer_creates_abc_draft_without_promoting(tmp_path: Path):
    registry = DomainRegistry(tmp_path / "domains")
    result = ExistingCapabilityImporter(registry=registry).create_domain_draft("abc_reasoning", "1.0.0")
    assert result["status"] == "draft"
    draft = Path(result["draft_path"])
    assert (draft / "capabilities.yaml").exists()
    manifest = yaml.safe_load((draft / "domain.yaml").read_text())
    assert manifest["state"] == "draft"
    assert "abc_relcalc" in manifest["deterministic_capabilities"]
    assert not (tmp_path / "domains" / "active" / "abc_reasoning" / "1.0.0").exists()


def test_importer_external_executors_reference_formula_without_copy(tmp_path: Path):
    registry = DomainRegistry(tmp_path / "domains")
    draft_path = Path(ExistingCapabilityImporter(registry=registry).create_domain_draft("abc_reasoning")["draft_path"])
    external = yaml.safe_load((draft_path / "rules" / "external_executors.yaml").read_text())
    formula = next(item for item in external["external_executors"] if item["capability_id"] == "abc_formula_loop")
    assert formula["canonical_executor"] == "openshell_backend/skills/abc_formula_loop.py"
    assert "calculate_scores" not in (draft_path / "rules" / "external_executors.yaml").read_text()


def test_importer_creates_bandi_draft_with_rulebook_source(tmp_path: Path):
    registry = DomainRegistry(tmp_path / "domains")
    draft_path = Path(ExistingCapabilityImporter(registry=registry).create_domain_draft("bandi_framework")["draft_path"])
    sources = (draft_path / "sources.jsonl").read_text()
    assert "regione_act_dimensioning.md" in sources
    manifest = yaml.safe_load((draft_path / "domain.yaml").read_text())
    assert manifest["state"] == "draft"
    assert manifest["domain_id"] == "bandi_framework"
    assert "regione_act_dimensioning" in manifest["deterministic_capabilities"]
    assert "Non e un bando operativo" in (draft_path / "manual.md").read_text()
