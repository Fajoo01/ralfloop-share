from __future__ import annotations

from pathlib import Path
from typing import Any

from .bandi_registry import BandoRegistry
from .calculation_orchestrator import CalculationOrchestrator
from .deterministic_engine import DeterministicEngine
from .models import DomainValidationResult
from .provenance import validate_rule_provenance
from .storage import read_jsonl, read_yaml, sha256_file, sha256_tree


class DomainValidator:
    def validate(self, domain: dict[str, Any] | str | Path) -> DomainValidationResult:
        loaded = self._load(domain)
        if not loaded:
            return DomainValidationResult(False, ["domain_not_found"])
        manifest = loaded["manifest"]
        blocking, warnings = [], []
        for field in ("domain_id", "version", "state", "scope", "out_of_scope", "minimum_source_count"):
            if not manifest.get(field):
                blocking.append(f"manifest_missing:{field}")
        sources = loaded.get("sources", [])
        source_ids = {s.get("source_id") for s in sources}
        sources_by_id = {s.get("source_id"): s for s in sources}
        if len(sources) < int(manifest.get("minimum_source_count", 1)):
            blocking.append("sources_present")
        source_results = {}
        for src in sources:
            checksum = src.get("checksum")
            path = Path(str(src.get("uri_or_path") or src.get("path") or ""))
            if path.is_file() and checksum and checksum != sha256_file(path):
                blocking.append(f"source_checksum_invalid:{src.get('source_id')}")
                source_results[src.get("source_id")] = "checksum_invalid"
            else:
                source_results[src.get("source_id")] = "ok"
        rules = loaded.get("rules", [])
        ids = [r.get("rule_id") for r in rules]
        if len(ids) != len(set(ids)):
            blocking.append("rule_ids_not_unique")
        for rule in rules:
            if int(rule.get("priority", 0)) < 0:
                blocking.append(f"rule_priority_invalid:{rule.get('rule_id')}")
            blocking.extend(validate_rule_provenance(rule, source_ids, sources_by_id))
        for table in loaded.get("decision_tables", []):
            if not table.get("table_id") or not isinstance(table.get("rows", []), list):
                blocking.append("decision_table_incomplete")
        conflicts = loaded.get("conflicts", [])
        if any(c.get("blocking") for c in conflicts):
            blocking.append("unresolved_blocking_conflicts")
        if not loaded.get("examples"):
            blocking.append("examples_present")
        if not loaded.get("tests"):
            blocking.append("tests_present")
        test_results = self._run_tests(loaded)
        pass_rate = test_results.get("pass_rate", 0.0)
        if loaded.get("tests") and pass_rate < float(manifest.get("minimum_test_pass_rate", 1.0)):
            blocking.append("minimum_test_pass_rate_not_met")
        if not manifest.get("jury_review_completed", False):
            blocking.append("jury_review_missing")
        if not manifest.get("red_team_completed", False):
            blocking.append("red_team_missing")
        if not loaded.get("fixture") and loaded.get("path"):
            path = Path(loaded["path"])
            stored = (path / "manifest.sha256").read_text(encoding="utf-8").strip() if (path / "manifest.sha256").exists() else ""
            if stored and stored != sha256_tree(path):
                blocking.append("content_hash_invalid")
        valid = not blocking
        return DomainValidationResult(valid, blocking, warnings, test_results, source_results, {"completed": True}, valid)

    def _run_tests(self, domain: dict[str, Any]) -> dict[str, Any]:
        tests = domain.get("tests", [])
        if not tests:
            return {"total": 0, "passed": 0, "pass_rate": 0.0}
        if domain.get("manifest", {}).get("bando_id"):
            return self._run_bando_tests(domain)
        engine = DeterministicEngine()
        passed = 0
        details = []
        for case in tests:
            result = engine.evaluate(domain, case.get("input", ""))
            ok = str(result.get("result")) == str(case.get("expected"))
            passed += int(ok)
            details.append({"test_id": case.get("test_id"), "passed": ok})
        return {"total": len(tests), "passed": passed, "pass_rate": passed / len(tests), "details": details}

    def _run_bando_tests(self, domain: dict[str, Any]) -> dict[str, Any]:
        manifest = domain["manifest"]
        bando_id = str(manifest["bando_id"])
        version = str(manifest["version"])
        root = Path(domain["path"]).parents[3] if domain.get("path") else Path("domains")
        registry = BandoRegistry(root)
        calculator = CalculationOrchestrator()
        passed = 0
        details = []
        for case in domain.get("tests", []):
            case_input = case.get("input", {})
            expected = case.get("expected", {})
            if case_input.get("calculation_id"):
                contribution_percent = float(case_input.get("contribution_rate", 0)) * 100
                contribution_percent_text = f"{contribution_percent:g}"
                calc = calculator.calculate(
                    {
                        "expression": f"{contribution_percent_text}% di {case_input.get('total_project_cost')}",
                        "domain_context": {"bando_id": bando_id, "source_refs": case.get("source_refs", [])},
                    }
                )
                ok = str(calc.get("answer")) == str(expected.get("result"))
            elif "version" in expected and case_input.get("bando_id"):
                found = registry.get_version(str(case_input["bando_id"]), version)
                ok = bool(found) and found.version == expected["version"]
            elif "goal" in case_input and (
                "calcola" in str(case_input["goal"]).lower() or "%" in str(case_input["goal"])
            ):
                calc = calculator.calculate(
                    {
                        "goal": str(case_input["goal"]),
                        "domain_context": {}
                        if expected.get("status") == "missing_context"
                        else {"bando_id": bando_id, "source_refs": case.get("source_refs", [])},
                    }
                )
                expected_core = {
                    key: value
                    for key, value in expected.items()
                    if key not in {"no_number_invented", "no_official_rule_invented"}
                }
                ok = all(calc.get(key) == value for key, value in expected_core.items())
                if expected.get("no_number_invented"):
                    ok = ok and calc.get("answer") is None
            elif "goal" in case_input and not case_input.get("bando_id"):
                resolution = registry.resolve_bando(str(case_input["goal"]))
                ok = expected.items() <= resolution.to_dict().items()
            else:
                result = registry.evaluate({"bando_id": bando_id, "version": version, **case_input}).to_dict()
                if case.get("kind") == "ambiguous":
                    ok = all(result.get(key) == value for key, value in expected.items() if key in result)
                    if "no_official_rule_invented" in expected:
                        ok = ok and result.get("status") == "uncovered_case"
                    if "no_number_invented" in expected:
                        ok = ok and result.get("status") in {"uncovered_case", "missing_context", "insufficient_input"}
                elif "contains" in expected:
                    values = result.get("result", {}).get("value", [])
                    ok = all(item in values for item in expected["contains"])
                elif "eligible" in expected:
                    values = result.get("result", {}).get("value", [])
                    if not isinstance(values, list):
                        values = [values]
                    raw = str(case_input.get("value", "")).lower()
                    matched = any(raw in str(item).lower() for item in values)
                    if "exclusion" in str(case_input.get("field", "")):
                        ok = matched and expected["eligible"] is False
                    else:
                        ok = matched == bool(expected["eligible"])
                else:
                    ok = all(result.get("result", {}).get(key) == value for key, value in expected.items())
            passed += int(ok)
            details.append({"test_id": case.get("test_id"), "passed": ok})
        total = len(domain.get("tests", []))
        return {"total": total, "passed": passed, "pass_rate": passed / total if total else 0.0, "details": details}

    def _load(self, domain: dict[str, Any] | str | Path) -> dict[str, Any] | None:
        if isinstance(domain, dict):
            return domain
        path = Path(domain)
        manifest = read_yaml(path / "domain.yaml")
        if not manifest:
            return None
        rules = []
        for file in (path / "rules").glob("*.yaml"):
            data = read_yaml(file)
            if isinstance(data.get("rules"), list):
                rules.extend(data["rules"])
            elif data:
                rules.append(data)
        decision_tables = []
        for file in (path / "decision_tables").glob("*.yaml"):
            decision_tables.append(read_yaml(file))
        return {"manifest": manifest, "rules": rules, "decision_tables": decision_tables, "sources": read_jsonl(path / "sources.jsonl"), "conflicts": read_jsonl(path / "conflicts.jsonl"), "tests": read_jsonl(path / "tests" / "tests.jsonl"), "examples": read_jsonl(path / "examples" / "examples.jsonl"), "path": str(path)}
