from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .models import DomainBuildResult, DomainManifest
from .provenance import source_record
from .registry import DomainRegistry
from .storage import append_jsonl, now_iso, sha256_tree, write_text_atomic, write_yaml_atomic


class DomainBuilder:
    def __init__(self, registry: DomainRegistry | None = None) -> None:
        self.registry = registry or DomainRegistry()

    def create_draft(self, seed_goal: str, source_inputs: list[str] | None = None, constraints: dict[str, Any] | None = None) -> DomainBuildResult:
        source_inputs = source_inputs or []
        constraints = constraints or {}
        domain_id = constraints.get("domain_id") or self._propose_id(seed_goal)
        version = constraints.get("version") or "0.1.0"
        draft_root = self.registry.root / "drafts" / domain_id / version
        sources = [Path(p) for p in source_inputs]
        existing = [p for p in sources if p.exists()]
        now = now_iso()
        state = "draft" if existing else "invalid"
        blocking = [] if existing else ["insufficient_sources"]
        manifest = DomainManifest(
            domain_id=domain_id,
            display_name=domain_id.replace("_", " ").title(),
            description=f"Draft generated from seed goal: {seed_goal}",
            version=version,
            state=state, created_at=now, updated_at=now, created_by="domain_builder",
            languages=["it"], scope=[seed_goal[:120]], out_of_scope=["side effects without approval"],
            source_requirements=["expert-authored_document"], deterministic_capabilities=[], jury_capabilities=["domain_creation_review"],
            external_action_policy="deny", rule_precedence=[], minimum_source_count=1, minimum_test_pass_rate=1.0, content_hash="", schema_version="1.0",
            jury_review_completed=bool(existing), red_team_completed=bool(existing),
        )
        self._write_draft(draft_root, manifest, seed_goal, existing, blocking)
        manifest_dict = manifest.to_dict()
        manifest_dict["content_hash"] = sha256_tree(draft_root)
        write_yaml_atomic(draft_root / "domain.yaml", manifest_dict)
        write_text_atomic(draft_root / "manifest.sha256", sha256_tree(draft_root) + "\n")
        if state == "draft":
            self.registry.register_draft(manifest_dict, draft_root)
        return DomainBuildResult(bool(existing), state, domain_id, version, str(draft_root), blocking, [], True)

    def _write_draft(self, root: Path, manifest: DomainManifest, seed_goal: str, sources: list[Path], blocking: list[str]) -> None:
        write_yaml_atomic(root / "domain.yaml", manifest.to_dict())
        write_text_atomic(root / "manual.md", f"# {manifest.display_name}\n\nScopo: {seed_goal}\n\nLimiti: nessuna regola operativa senza fonte.\n\nEscalation: usare giuria solo per review bozza.\n")
        write_text_atomic(root / "glossary.md", "# Glossary\n\nDraft terms extracted after source validation.\n")
        for d in ("rules", "decision_tables", "examples", "tests"):
            (root / d).mkdir(parents=True, exist_ok=True)
        if sources:
            append_jsonl(root / "examples" / "examples.jsonl", {"example_id": "draft_scope_example", "kind": "edge", "input": seed_goal, "expected": "requires_domain_review", "notes": "Draft example, not an operational rule"})
            append_jsonl(root / "tests" / "tests.jsonl", {"test_id": "draft_no_operational_answer", "input": seed_goal, "expected": None, "rule_refs": []})
        write_text_atomic(root / "CHANGELOG.md", "# Changelog\n\n- draft created\n")
        for i, source in enumerate(sources, 1):
            append_jsonl(root / "sources.jsonl", source_record(f"source_{i}", source))
        if blocking:
            append_jsonl(root / "conflicts.jsonl", {"conflict_id": "blocking_sources", "blocking": True, "description": ",".join(blocking)})
        else:
            write_text_atomic(root / "conflicts.jsonl", "")

    def _propose_id(self, seed_goal: str) -> str:
        words = re.findall(r"[a-zA-Z0-9]+", seed_goal.lower())[:5]
        return "domain_" + "_".join(words or ["draft"])
