from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ralfloop_agent.domains.capability_registry import CanonicalCapabilityRegistry


def test_mapping_loads_and_has_expected_capabilities():
    registry = CanonicalCapabilityRegistry.from_mapping()
    ids = {item["capability_id"] for item in registry.list_capabilities()}
    assert {"abc_relcalc", "abc_formula_loop", "abc_rulebooks", "regione_act_dimensioning", "grammar"} <= ids
    assert registry.get("abc_relcalc").deterministic_level == "deterministic_with_structured_input"
    assert registry.get("abc_rulebooks").deterministic_level == "source_only"


def test_mapping_sources_verify_against_declared_checksums():
    report = CanonicalCapabilityRegistry.from_mapping().verify_sources()
    assert report["ok"] is True
    assert report["status"] == "ok"


def test_mapping_detects_duplicate_executor(tmp_path: Path):
    base = yaml.safe_load(Path("config/domain_capability_mapping.yaml").read_text())
    duplicate = copy.deepcopy(base["capabilities"][0])
    duplicate["capability_id"] = "abc_relcalc_duplicate"
    base["capabilities"].append(duplicate)
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(yaml.safe_dump(base), encoding="utf-8")
    report = CanonicalCapabilityRegistry.from_mapping(mapping).validate_mapping()
    assert report["ok"] is False
    assert any(item["type"] == "duplicate" for item in report["issues"])


def test_checksum_change_marks_validation_required(tmp_path: Path):
    base = yaml.safe_load(Path("config/domain_capability_mapping.yaml").read_text())
    base["capabilities"][0]["provenance"]["source_checksums"]["openshell_backend/skills/abc_relcalc.py"] = "bad"
    mapping = tmp_path / "mapping.yaml"
    mapping.write_text(yaml.safe_dump(base), encoding="utf-8")
    report = CanonicalCapabilityRegistry.from_mapping(mapping).verify_sources()
    assert report["ok"] is False
    assert report["status"] == "validation_required"
    assert "openshell_backend/skills/abc_relcalc.py" in report["changed"]


def test_capability_resolution_exact_domain():
    registry = CanonicalCapabilityRegistry.from_mapping()
    resolution = registry.resolve("abc_reasoning", {"capability_id": "abc_relcalc"})
    assert resolution.status == "resolved"
    assert resolution.capability_id == "abc_relcalc"


def test_grammar_remains_manual_rag_not_deterministic_executor():
    grammar = CanonicalCapabilityRegistry.from_mapping().get("grammar")
    assert grammar.deterministic_level == "non_deterministic"
    assert grammar.adapter == "manual_rag"
    assert grammar.capability_scope == "evidence_only"
    assert grammar.deterministic_core == "none"
