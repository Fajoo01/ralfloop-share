from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from ralfloop_agent.domains.bandi_registry import BandoRegistry
from ralfloop_agent.domains.calculation_orchestrator import CalculationOrchestrator
from ralfloop_agent.domains.storage import read_jsonl, sha256_tree
from ralfloop_agent.domains.validator import DomainValidator


BANDO_ID = "fondazione_unipolis_act_2026"
VERSION = "1.0.0"
DOMAIN_PATH = Path("domains/drafts/bandi") / BANDO_ID / VERSION


def _manifest() -> dict:
    return yaml.safe_load((DOMAIN_PATH / "domain.yaml").read_text(encoding="utf-8"))


def test_first_real_bando_identity_and_state():
    manifest = _manifest()

    assert manifest["domain_id"] == f"bandi/{BANDO_ID}"
    assert manifest["bando_id"] == BANDO_ID
    assert manifest["issuer"] == "Fondazione Unipolis"
    assert manifest["edition"] == "2026"
    assert manifest["version"] == VERSION
    assert manifest["state"] == "draft"
    assert manifest["deadline"] == "2026-04-09T13:00:00+02:00"
    assert not (Path("domains/active/bandi") / BANDO_ID / VERSION).exists()


def test_sources_separate_binding_official_from_supporting_documents():
    sources = {item["source_id"]: item for item in read_jsonl(DOMAIN_PATH / "sources.jsonl")}

    assert sources["act_regolamento_2026"]["source_type"] == "primary_official_document"
    assert sources["act_regolamento_2026"]["binding_level"] == "binding_official"
    assert sources["act_regolamento_2026"]["checksum"]
    assert sources["act_formulario_draft_2026"]["binding_level"] == "supporting_not_binding"
    assert sources["act_budget_draft"]["binding_level"] == "supporting_not_binding"
    assert sources["act_quadro_logico_draft"]["binding_level"] == "supporting_not_binding"


def test_all_rules_have_regolamento_provenance_only():
    for file in sorted((DOMAIN_PATH / "rules").glob("*.yaml")):
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        for rule in data.get("rules", []):
            assert rule["bando_id"] == BANDO_ID
            assert rule["version"] == VERSION
            assert rule["source_refs"] == ["act_regolamento_2026"]
            assert rule["source_sections"]
            assert "act_formulario_draft_2026" not in rule["source_refs"]


def test_registry_resolves_and_evaluates_explicit_deadline_without_jury():
    registry = BandoRegistry()
    resolved = registry.resolve_bando("Bando ACT 2026 Fondazione Unipolis")
    result = registry.evaluate({"bando_id": BANDO_ID, "version": VERSION, "goal": "Qual è la scadenza del bando?"})

    assert resolved.status == "resolved"
    assert resolved.bando_id == BANDO_ID
    assert result.status == "completed"
    assert result.deterministic is True
    assert result.jury_required is False
    assert result.result == {"field": "deadline", "value": "2026-04-09T13:00:00+02:00", "rule_id": "application_deadline"}
    assert result.source_refs == ["act_regolamento_2026"]


def test_uncovered_expense_needs_jury_but_invents_no_rule():
    registry = BandoRegistry()
    before = len(registry.get_effective_rules(BANDO_ID, VERSION).rules)
    result = registry.evaluate({"bando_id": BANDO_ID, "version": VERSION, "goal": "La spesa per catering sperimentale è ammissibile?"})
    after = len(registry.get_effective_rules(BANDO_ID, VERSION).rules)

    assert result.status == "uncovered_case"
    assert result.jury_required is True
    assert result.jury_reason_codes == ["incomplete_rules"]
    assert result.result is None
    assert before == after


def test_calculation_example_is_deterministic_and_capped_by_official_context():
    out = CalculationOrchestrator().calculate(
        {
            "expression": "75% di 100000",
            "domain_context": {"bando_id": BANDO_ID, "source_refs": ["act_regolamento_2026"]},
        }
    )

    assert out["status"] == "completed"
    assert out["answer"] == "75000"
    assert out["deterministic"] is True
    assert out["jury_required"] is False
    assert out["sources"] == ["act_regolamento_2026"]


def test_domain_validator_runs_bando_tests_but_keeps_draft_not_active():
    result = DomainValidator().validate(DOMAIN_PATH)

    assert result.test_results["passed"] == result.test_results["total"]
    assert "content_hash_invalid" not in result.blocking_issues
    assert "source_checksum_invalid:act_regolamento_2026" not in result.blocking_issues
    assert result.ready_for_approval is False
    assert _manifest()["state"] == "draft"


def test_registry_tracks_draft_without_active_route():
    registry = json.loads(Path("domains/registry.json").read_text(encoding="utf-8"))
    rows = [row for row in registry["domains"] if row["domain_id"] == f"bandi/{BANDO_ID}"]

    assert rows == [
        {
            "domain_id": f"bandi/{BANDO_ID}",
            "version": VERSION,
            "state": "draft",
            "path": str(Path.cwd() / DOMAIN_PATH),
        }
    ]


def test_manifest_hash_matches_tree():
    assert (DOMAIN_PATH / "manifest.sha256").read_text(encoding="utf-8").strip() == sha256_tree(DOMAIN_PATH)


def test_bandi_cli_evaluate_reports_disabled_jury_for_ambiguous_case():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "ralfloop_agent.domains.cli",
            "bandi",
            "evaluate",
            "--bando",
            BANDO_ID,
            "--version",
            VERSION,
            "--goal",
            "La spesa per catering sperimentale è ammissibile?",
        ],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    out = json.loads(proc.stdout)

    assert proc.returncode == 4
    assert out["status"] == "uncovered_case"
    assert out["jury_required"] is True
    assert out["jury_status"] == "disabled"
    assert out["deterministic_complete"] is False
