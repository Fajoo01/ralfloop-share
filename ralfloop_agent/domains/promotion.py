from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from .models import DomainPromotionResult
from .registry import DomainRegistry
from .storage import append_jsonl, now_iso, read_yaml, sha256_tree, write_text_atomic, write_yaml_atomic
from .validator import DomainValidator


class DomainPromotionService:
    def __init__(self, registry: DomainRegistry | None = None) -> None:
        self.registry = registry or DomainRegistry()

    def promote(self, domain_id: str, version: str, *, approved_by: str, approval_token: str | None, approval_reason: str = "") -> DomainPromotionResult:
        expected = os.getenv("DOMAIN_APPROVAL_TOKEN")
        if not expected or approval_token != expected:
            return DomainPromotionResult(False, "approval_required", domain_id, version, blocking_issues=["invalid_or_missing_approval_token"], approval_required=True)
        draft = self.registry.get_domain(domain_id, version)
        if not draft or draft["manifest"].get("state") != "draft":
            return DomainPromotionResult(False, "draft_not_found", domain_id, version, blocking_issues=["draft_not_found"])
        validation = DomainValidator().validate(draft)
        if not validation.valid:
            return DomainPromotionResult(False, "validation_failed", domain_id, version, blocking_issues=validation.blocking_issues)
        src = Path(draft["path"])
        dest = self.registry.root / "active" / domain_id / version
        if dest.exists():
            return DomainPromotionResult(False, "active_version_exists", domain_id, version, blocking_issues=["active_version_exists"])
        tmp = dest.with_name(dest.name + f".{os.getpid()}.tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(src, tmp)
        manifest = read_yaml(tmp / "domain.yaml")
        manifest.update({"state": "active", "approved_by": approved_by, "approved_at": now_iso(), "updated_at": now_iso()})
        write_yaml_atomic(tmp / "domain.yaml", manifest)
        write_text_atomic(tmp / "manifest.sha256", sha256_tree(tmp) + "\n")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(tmp, dest)
        self.registry.promote(domain_id, version, dest)
        event = {"event": "promote", "domain_id": domain_id, "version": version, "approved_by": approved_by, "reason": approval_reason, "timestamp": now_iso()}
        append_jsonl(self.registry.root / "promotion_audit.jsonl", event)
        return DomainPromotionResult(True, "active", domain_id, version, str(dest), False, [], event)
