from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from .models import DomainManifest
from .storage import read_json, read_jsonl, read_yaml, sha256_tree, write_json_atomic


class DomainRegistry:
    def __init__(self, root: str | Path = "domains") -> None:
        self.root = Path(root)
        self.registry_path = self.root / "registry.json"
        for name in ("active", "drafts", "deprecated", "quarantine"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def list_domains(self, state: str | None = None) -> list[dict[str, Any]]:
        index = self._index()
        rows = list(index.get("domains", []))
        return [row for row in rows if state is None or row.get("state") == state]

    def get_domain(self, domain_id: str, version: str | None = None) -> dict[str, Any] | None:
        rows = [row for row in self.list_domains() if row.get("domain_id") == domain_id and (version is None or row.get("version") == version)]
        if not rows:
            fixture = fixture_domain(domain_id)
            if fixture and (version is None or fixture["manifest"]["version"] == version):
                return fixture
            return None
        if version is None:
            active_rows = [row for row in rows if row.get("state") == "active"]
            rows = active_rows or rows
            rows.sort(key=lambda item: item.get("version", ""))
            return self._load_row(rows[-1])
        state_order = {"draft": 0, "validating": 1, "approval_required": 2, "active": 3, "deprecated": 4, "quarantined": 5, "invalid": 6}
        rows.sort(key=lambda item: state_order.get(str(item.get("state")), 99))
        return self._load_row(rows[0])

    def find_active(self, domain_id: str) -> dict[str, Any] | None:
        rows = [row for row in self.list_domains("active") if row.get("domain_id") == domain_id]
        if not rows:
            fixture = fixture_domain(domain_id)
            if fixture and fixture["manifest"]["state"] == "active":
                return fixture
            return None
        rows.sort(key=lambda item: item.get("version", ""))
        return self._load_row(rows[-1])

    def register_draft(self, manifest: DomainManifest | dict[str, Any], path: str | Path) -> dict[str, Any]:
        data = asdict(manifest) if isinstance(manifest, DomainManifest) else dict(manifest)
        if data.get("state") != "draft":
            data["state"] = "draft"
        row = {"domain_id": data["domain_id"], "version": data["version"], "state": "draft", "path": str(path)}
        index = self._index()
        index["domains"] = [item for item in index.get("domains", []) if not (item.get("domain_id") == row["domain_id"] and item.get("version") == row["version"] and item.get("state") == "draft")]
        index["domains"].append(row)
        write_json_atomic(self.registry_path, index)
        return row

    def promote(self, domain_id: str, version: str, active_path: str | Path) -> dict[str, Any]:
        row = {"domain_id": domain_id, "version": version, "state": "active", "path": str(active_path)}
        index = self._index()
        index["domains"] = [item for item in index.get("domains", []) if not (item.get("domain_id") == domain_id and item.get("version") == version and item.get("state") == "active")]
        index["domains"].append(row)
        write_json_atomic(self.registry_path, index)
        return row

    def deprecate(self, domain_id: str, version: str) -> bool:
        return self._set_state(domain_id, version, "deprecated")

    def quarantine(self, domain_id: str, version: str) -> bool:
        return self._set_state(domain_id, version, "quarantined")

    def verify_integrity(self, domain_id: str, version: str | None = None) -> dict[str, Any]:
        domain = self.get_domain(domain_id, version)
        if not domain:
            return {"ok": False, "error": "domain_missing"}
        if domain.get("fixture"):
            return {"ok": True, "fixture": True}
        path = Path(domain["path"])
        stored = (path / "manifest.sha256").read_text(encoding="utf-8").strip() if (path / "manifest.sha256").exists() else ""
        actual = sha256_tree(path)
        ok = stored == actual
        if not ok:
            self.quarantine(domain["manifest"]["domain_id"], domain["manifest"]["version"])
        return {"ok": ok, "stored": stored, "actual": actual}

    def rebuild_index(self) -> dict[str, Any]:
        rows = []
        for state in ("active", "drafts", "deprecated", "quarantine"):
            for manifest_path in (self.root / state).glob("*/*/domain.yaml"):
                manifest = read_yaml(manifest_path)
                rows.append({"domain_id": manifest.get("domain_id"), "version": manifest.get("version"), "state": manifest.get("state"), "path": str(manifest_path.parent)})
        index = {"domains": rows}
        write_json_atomic(self.registry_path, index)
        return index

    def _index(self) -> dict[str, Any]:
        data = read_json(self.registry_path)
        if not data:
            data = {"domains": []}
        return data

    def _load_row(self, row: dict[str, Any]) -> dict[str, Any] | None:
        path = Path(str(row.get("path")))
        manifest = read_yaml(path / "domain.yaml")
        if not manifest or manifest.get("state") == "quarantined":
            return None
        rules = [read_yaml(file) for file in sorted((path / "rules").glob("*.yaml"))]
        decision_tables = [read_yaml(file) for file in sorted((path / "decision_tables").glob("*.yaml"))]
        return {
            "manifest": manifest,
            "rules": [rule for rule in rules if rule],
            "decision_tables": [table for table in decision_tables if table],
            "examples": read_jsonl(path / "examples" / "examples.jsonl"),
            "tests": read_jsonl(path / "tests" / "tests.jsonl"),
            "sources": read_jsonl(path / "sources.jsonl"),
            "conflicts": read_jsonl(path / "conflicts.jsonl"),
            "path": str(path),
            "fixture": False,
        }

    def _set_state(self, domain_id: str, version: str, state: str) -> bool:
        index = self._index()
        changed = False
        for row in index.get("domains", []):
            if row.get("domain_id") == domain_id and row.get("version") == version:
                row["state"] = state
                changed = True
        if changed:
            write_json_atomic(self.registry_path, index)
        return changed


def fixture_domain(domain_id: str) -> dict[str, Any] | None:
    if domain_id == "arithmetic_basic":
        manifest = {
            "domain_id": "arithmetic_basic", "display_name": "Arithmetic Basic", "description": "Integer addition fixture", "version": "1.0.0", "state": "active",
            "created_at": "2026-07-11T00:00:00Z", "updated_at": "2026-07-11T00:00:00Z", "created_by": "fixture", "approved_by": "fixture", "approved_at": "2026-07-11T00:00:00Z",
            "languages": ["it", "en"], "scope": ["somme intere", "integer addition", "quanto fa"], "out_of_scope": ["probabilità", "strategia"],
            "source_requirements": ["approved_local_policy"], "deterministic_capabilities": ["integer_addition"], "jury_capabilities": [], "external_action_policy": "deny",
            "rule_precedence": ["add_integer_pair"], "minimum_source_count": 1, "minimum_test_pass_rate": 1.0, "content_hash": "fixture", "schema_version": "1.0", "aliases": ["arithmetic", "calcolo", "somma"], "jury_review_completed": True, "red_team_completed": True,
        }
        rules = [{"rule_id": "add_integer_pair", "description": "Add two integers", "priority": 10, "conditions": {"intent": "integer_addition"}, "action": {"operation": "sum"}, "exceptions": [], "source_refs": ["local_arithmetic_policy"], "confidence": 1.0, "blocking": False, "enabled": True, "rule_type": "local_operational_policy"}]
        sources = [{"source_id": "local_arithmetic_policy", "title": "Arithmetic fixture policy", "source_type": "ralfloop_local_policy", "uri_or_path": "fixture://arithmetic_basic", "publisher": "ralfloop", "version": "1", "checksum": "fixture", "reliability": 1.0, "sections_used": ["integer addition"], "notes": "test fixture"}]
        tests = [{"test_id": "two_plus_three", "input": "Quanto fa 2 + 3?", "expected": "5", "rule_refs": ["add_integer_pair"]}]
        examples = [{"example_id": "sum_positive", "kind": "positive", "input": "Quanto fa 2 + 3?", "expected": "5"}]
        return {"manifest": manifest, "rules": rules, "sources": sources, "examples": examples, "tests": tests, "path": "fixture://arithmetic_basic", "fixture": True}
    if domain_id == "incident_triage":
        manifest = {
            "domain_id": "incident_triage", "display_name": "Incident Triage", "description": "Mixed incident triage fixture", "version": "1.0.0", "state": "active",
            "created_at": "2026-07-11T00:00:00Z", "updated_at": "2026-07-11T00:00:00Z", "created_by": "fixture", "approved_by": "fixture", "approved_at": "2026-07-11T00:00:00Z",
            "languages": ["it", "en"], "scope": ["incidente", "incident", "severity", "triage", "strategia prudente"], "out_of_scope": ["medicina", "finanza"],
            "source_requirements": ["approved_local_policy"], "deterministic_capabilities": ["severity_lookup"], "jury_capabilities": ["strategy_recommendation", "evidence_synthesis"], "external_action_policy": "deny",
            "rule_precedence": ["sev1_down"], "minimum_source_count": 1, "minimum_test_pass_rate": 1.0, "content_hash": "fixture", "schema_version": "1.0", "aliases": ["incident", "triage", "incidente"], "jury_review_completed": True, "red_team_completed": True,
        }
        rules = [{"rule_id": "sev1_down", "description": "Service down with many users is sev1", "priority": 100, "conditions": {"contains_all": ["down", "utenti"]}, "action": {"severity": "sev1"}, "exceptions": [], "source_refs": ["local_incident_policy"], "confidence": 1.0, "blocking": False, "enabled": True, "rule_type": "local_operational_policy"}]
        sources = [{"source_id": "local_incident_policy", "title": "Incident triage fixture policy", "source_type": "ralfloop_local_policy", "uri_or_path": "fixture://incident_triage", "publisher": "ralfloop", "version": "1", "checksum": "fixture", "reliability": 1.0, "sections_used": ["severity"], "notes": "test fixture"}]
        conflicts = [{"conflict_id": "response_speed_vs_safety", "blocking": False, "description": "Speed and safety tradeoff requires qualitative assessment"}]
        examples = [{"example_id": "incident_strategy", "kind": "edge", "input": "Quale strategia prudente?", "expected": "jury_required"}]
        tests = [{"test_id": "sev1_down", "input": "servizio down molti utenti", "expected": "{'severity': 'sev1'}", "rule_refs": ["sev1_down"]}]
        return {"manifest": manifest, "rules": rules, "sources": sources, "conflicts": conflicts, "examples": examples, "tests": tests, "path": "fixture://incident_triage", "fixture": True}
    return None
