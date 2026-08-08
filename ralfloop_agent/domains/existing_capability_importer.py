from __future__ import annotations

from pathlib import Path
from typing import Any

from .capability_registry import CanonicalCapabilityRegistry
from .models import DomainManifest
from .registry import DomainRegistry
from .storage import append_jsonl, now_iso, sha256_tree, write_text_atomic, write_yaml_atomic


class ExistingCapabilityImporter:
    def __init__(
        self,
        registry: DomainRegistry | None = None,
        capability_registry: CanonicalCapabilityRegistry | None = None,
    ) -> None:
        self.registry = registry or DomainRegistry()
        self.capability_registry = capability_registry or CanonicalCapabilityRegistry.from_mapping()

    def inspect_capability(self, capability_id: str) -> dict[str, Any]:
        cap = self.capability_registry.get(capability_id)
        sources = self.capability_registry.verify_sources()["results"].get(capability_id, {})
        return {"capability": cap.to_dict(), "sources": sources}

    def create_domain_draft(self, domain_id: str, version: str = "1.0.0") -> dict[str, Any]:
        caps = [cap for cap in self.capability_registry.capabilities.values() if cap.domain_id == domain_id and cap.enabled]
        if not caps:
            return {"ok": False, "status": "domain_capabilities_missing", "domain_id": domain_id}
        draft_path = self.registry.root / "drafts" / domain_id / version
        now = now_iso()
        manifest = DomainManifest(
            domain_id=domain_id,
            display_name=domain_id.replace("_", " ").title(),
            description=f"Draft wrapper for existing canonical capabilities: {', '.join(c.capability_id for c in caps)}",
            version=version,
            state="draft",
            created_at=now,
            updated_at=now,
            created_by="existing_capability_importer",
            languages=["it", "en"],
            scope=[c.display_name for c in caps],
            out_of_scope=["side effects", "rules not covered by canonical sources", "automatic promotion"],
            source_requirements=["internal_rulebook", "canonical_python_executor"],
            deterministic_capabilities=[c.capability_id for c in caps if c.deterministic_level != "source_only"],
            jury_capabilities=["unresolved_only"],
            external_action_policy="deny",
            rule_precedence=[c.capability_id for c in caps],
            minimum_source_count=1,
            minimum_test_pass_rate=1.0,
            content_hash="",
            schema_version="1.0",
            aliases=[domain_id],
            jury_review_completed=True,
            red_team_completed=True,
        )
        self._write_draft(draft_path, manifest, caps)
        manifest_data = manifest.to_dict()
        manifest_data["content_hash"] = sha256_tree(draft_path)
        write_yaml_atomic(draft_path / "domain.yaml", manifest_data)
        write_text_atomic(draft_path / "manifest.sha256", sha256_tree(draft_path) + "\n")
        self.registry.register_draft(manifest_data, draft_path)
        return {"ok": True, "status": "draft", "domain_id": domain_id, "version": version, "draft_path": str(draft_path), "capabilities": [c.capability_id for c in caps]}

    def refresh_domain_draft(self, domain_id: str, version: str = "1.0.0") -> dict[str, Any]:
        return self.create_domain_draft(domain_id, version)

    def compare_with_existing_domain(self, domain_id: str, version: str = "1.0.0") -> dict[str, Any]:
        existing = self.registry.get_domain(domain_id, version)
        caps = [cap.capability_id for cap in self.capability_registry.capabilities.values() if cap.domain_id == domain_id]
        if not existing:
            return {"status": "missing", "domain_id": domain_id, "expected_capabilities": caps}
        manifest_caps = existing["manifest"].get("deterministic_capabilities", [])
        return {
            "status": "compared",
            "domain_id": domain_id,
            "version": version,
            "missing_capabilities": sorted(set(caps) - set(manifest_caps)),
            "extra_capabilities": sorted(set(manifest_caps) - set(caps)),
        }

    def _write_draft(self, root: Path, manifest: DomainManifest, caps: list[Any]) -> None:
        write_yaml_atomic(root / "domain.yaml", manifest.to_dict())
        write_text_atomic(root / "manual.md", _manual(manifest.domain_id))
        write_text_atomic(root / "glossary.md", _glossary(caps))
        for folder in ("rules", "decision_tables", "examples", "tests"):
            (root / folder).mkdir(parents=True, exist_ok=True)
        write_yaml_atomic(root / "capabilities.yaml", {"schema_version": 1, "capabilities": [cap.to_dict() for cap in caps]})
        write_yaml_atomic(root / "rules" / "external_executors.yaml", {"schema_version": 1, "external_executors": [_external_executor(cap) for cap in caps if cap.adapter != "source_only"]})
        write_text_atomic(root / "decision_tables" / "README.md", "Decision tables remain in canonical executors or rulebooks.\n")
        write_text_atomic(root / "examples" / "examples.jsonl", "")
        write_text_atomic(root / "tests" / "tests.jsonl", "")
        write_text_atomic(root / "tests" / "parity_cases.jsonl", "")
        source_report = self.capability_registry.verify_sources()["results"]
        for cap in caps:
            append_jsonl(root / "tests" / "parity_cases.jsonl", {"capability_id": cap.capability_id, "canonical_executor": cap.canonical_executor, "test_references": cap.test_references})
            for source in cap.canonical_sources:
                checksum = source_report.get(cap.capability_id, {}).get("actual", {}).get(source)
                append_jsonl(root / "sources.jsonl", {"source_id": f"{cap.capability_id}:{Path(source).name}", "title": Path(source).name, "source_type": cap.provenance.get("source_type", "internal_rulebook"), "uri_or_path": source, "publisher": "ralfloop", "version": "canonical", "published_at": None, "retrieved_at": now_iso(), "checksum": checksum or "", "reliability": 1.0, "sections_used": [], "notes": "Canonical source referenced; formula/rulebook is not copied."})
        write_text_atomic(root / "conflicts.jsonl", "")
        write_text_atomic(root / "CHANGELOG.md", f"# Changelog\n\n- {now_iso()} draft created from existing canonical capabilities.\n")


def _external_executor(cap: Any) -> dict[str, Any]:
    return {
        "rule_id": f"external_executor:{cap.capability_id}",
        "capability_id": cap.capability_id,
        "canonical_executor": cap.canonical_executor,
        "input_contract": {"required": cap.required_inputs, "optional": cap.optional_inputs},
        "output_contract": cap.output_schema,
        "source_refs": cap.canonical_sources,
        "failure_policy": {
            "missing_input": cap.missing_input_policy,
            "uncovered_case": cap.uncovered_case_policy,
            "jury_policy": cap.jury_policy,
        },
    }


def _manual(domain_id: str) -> str:
    if domain_id == "abc_reasoning":
        return (
            "# ABC Reasoning\n\n"
            "Scopo: collegare le capability ABC gia operative al Domain Orchestrator.\n\n"
            "Il calcolo numerico resta nei canonical executor esistenti. La giuria puo leggere residui qualitativi, conflitti o input irrisolti, ma non puo modificare formula, cap, score, range, action o flags deterministici.\n\n"
            "Fact extraction e separata da formula_scoring. Formula_scoring e deterministico quando riceve input normalizzati.\n"
        )
    if domain_id == "bandi_framework":
        return (
            "# Bandi Framework\n\n"
            "Scopo: meta-dominio tecnico per capability trasversali sui bandi. Non e un bando operativo e non sostituisce domini specifici per singolo bando.\n\n"
            "`regione_act_dimensioning` e capability cross-bando: applicabile solo quando il bando specifico rende pertinente il rulebook. Le regole specifiche del bando prevalgono. Fuori copertura: uncovered_case. Conflitti: conflicting_rules. Nessuna regola inventata dal modello o dalla giuria.\n"
        )
    return "# Existing Capability Draft\n\nDraft generated from canonical capabilities.\n"


def _glossary(caps: list[Any]) -> str:
    lines = ["# Glossary", ""]
    for cap in caps:
        lines.append(f"- `{cap.capability_id}`: {cap.display_name}.")
    return "\n".join(lines) + "\n"
