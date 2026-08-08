from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .domain_approval import (
    DomainApprovalPolicy,
    DomainApprovalScope,
    approval_status_response,
    effective_approval_status,
    expired_execution_response,
    hash_path,
    hash_value,
)
from .domain_approval_store import DomainApprovalStore
from .storage import read_json, read_jsonl, read_yaml, sha256_tree, write_json_atomic, write_text_atomic, write_yaml_atomic


def build_approval_scope(
    *,
    action: str,
    bando_id: str,
    version: str,
    canary_plan: dict[str, Any] | None = None,
    root: str | Path = "domains",
) -> dict[str, Any]:
    root = Path(root)
    draft = root / "drafts" / "bandi" / bando_id / version
    active = root / "active" / "bandi" / bando_id / version
    domain_path = draft if draft.exists() else active
    manifest = read_yaml(domain_path / "domain.yaml") if domain_path.exists() else {}
    scope = DomainApprovalScope(
        action=action,
        bando_id=bando_id,
        version=version,
        git_commit=_git_commit(),
        domain_state=str(manifest.get("state") or ""),
        domain_path=str(domain_path),
        source_draft_path=str(draft),
        destination_active_path=str(active),
        registry_path=str(root / "registry.json"),
        domain_manifest_hash=hash_path(domain_path / "domain.yaml"),
        domain_content_hash=hash_path(domain_path),
        source_set_hash=hash_path(domain_path / "sources.jsonl"),
        rule_set_hash=hash_path(domain_path / "rules"),
        test_evidence_hash=hash_path(domain_path / "tests"),
        canary_plan_hash=hash_value(canary_plan or {}),
        promotion_readiness_hash=hash_path(domain_path / "promotion-readiness.json"),
        canary_plan=canary_plan or {},
        validation_summary=_read_optional_json(domain_path / "validation.json"),
        known_uncovered_cases=read_jsonl(domain_path / "tests" / "ambiguous_cases.jsonl"),
        known_conflicts=read_jsonl(domain_path / "conflicts.jsonl"),
    )
    return scope.to_dict()


def request_domain_approval(
    *,
    action: str,
    bando_id: str,
    version: str,
    canary_plan: dict[str, Any] | None = None,
    send_telegram: bool = False,
    policy: DomainApprovalPolicy | None = None,
    store: DomainApprovalStore | None = None,
    root: str | Path = "domains",
) -> dict[str, Any]:
    policy = policy or DomainApprovalPolicy.from_env()
    if not policy.enabled:
        return {"status": "telegram_approval_gate_disabled", "enabled": False}
    gate = _policy_gate(action, bando_id, version, canary_plan, root)
    if not gate["ok"]:
        return gate
    scope = build_approval_scope(action=action, bando_id=bando_id, version=version, canary_plan=canary_plan, root=root)
    store = store or DomainApprovalStore(policy=policy)
    out = store.create_request(action=action, bando_id=bando_id, version=version, scope=scope)
    if send_telegram and out.get("request"):
        out["telegram_delivery"] = _write_outbox(out["request"], policy)
    return out


def approval_status(request_id: str, *, store: DomainApprovalStore | None = None) -> dict[str, Any]:
    store = store or DomainApprovalStore()
    row = store.get_request(request_id)
    if not row:
        return {"status": "not_found", "request_id": request_id}
    return approval_status_response(row)


def cancel_approval(request_id: str, *, store: DomainApprovalStore | None = None) -> dict[str, Any]:
    return (store or DomainApprovalStore()).cancel(request_id)


def execute_approved(request_id: str, *, dry_run: bool = False, store: DomainApprovalStore | None = None) -> dict[str, Any]:
    store = store or DomainApprovalStore()
    row = store.get_request(request_id)
    if not row:
        return {"status": "not_found", "request_id": request_id}
    effective = effective_approval_status(row)
    if effective == "consumed":
        return {"status": "already_consumed", "request_id": request_id}
    if effective == "expired":
        result = expired_execution_response(row)
        store.audit(
            "execution_blocked_expired",
            request_id=request_id,
            action=row["action"],
            bando_id=row["bando_id"],
            version=row["domain_version"],
            scope_digest=row["scope_digest"],
            old_status=row["status"],
            new_status="expired",
            result="expired",
        )
        return result
    if effective != "approved":
        return {"status": "approval_required", "request_id": request_id, "current_status": effective, "stored_status": row["status"], "effective_status": effective, "execution_allowed": False}
    stale = _stale_reasons(row)
    if stale:
        return store.mark_stale(request_id, stale)
    action = row["action"]
    store.record_execution(request_id, action, dry_run, "execution_started", {"dry_run": dry_run})
    if dry_run:
        result = {"status": "dry_run", "request_id": request_id, "action": action, "would_execute": True, "consumed": False}
        store.record_execution(request_id, action, True, "dry_run", result)
        return result
    if action == "promote_domain":
        result = _execute_promote(row)
    elif action == "run_domain_canary":
        result = _execute_run_domain_canary(row)
    elif action == "apply_domain_source_update":
        result = _execute_apply_domain_source_update(row)
    else:
        result = {"status": "unsupported_action", "request_id": request_id, "action": action}
    store.record_execution(request_id, action, False, result["status"], result)
    if result["status"] not in {"unsupported_action", "execution_failed"}:
        consume = store.consume(request_id, result)
        result["consume_status"] = consume["status"]
    return result


def _execute_run_domain_canary(row: dict[str, Any]) -> dict[str, Any]:
    return {"status": "canary_authorized", "request_id": row["request_id"], "action": row["action"], "canary_plan": row["scope"].get("canary_plan") or {}}


def _execute_apply_domain_source_update(row: dict[str, Any]) -> dict[str, Any]:
    return {"status": "source_update_authorized", "request_id": row["request_id"], "action": row["action"]}


def _policy_gate(action: str, bando_id: str, version: str, canary_plan: dict[str, Any] | None, root: str | Path) -> dict[str, Any]:
    if action == "run_domain_canary":
        plan = canary_plan or {}
        if not plan.get("expected_rule_id") and not plan.get("expected_rule_ids"):
            return {"ok": False, "status": "canary_policy_denied", "reason": "missing_expected_rule"}
        if not (plan.get("expected_deterministic_answer") or plan.get("expected_answer")):
            return {"ok": False, "status": "canary_policy_denied", "reason": "missing_expected_answer"}
        if bool(plan.get("jury_expected")) or bool(plan.get("recursive_mas_expected")):
            return {"ok": False, "status": "canary_policy_denied", "reason": "jury_not_allowed_for_initial_canary"}
    if action == "promote_domain":
        path = Path(root) / "drafts" / "bandi" / bando_id / version
        manifest = read_yaml(path / "domain.yaml")
        if manifest.get("state") != "draft":
            return {"ok": False, "status": "promotion_policy_denied", "reason": "domain_not_draft"}
        if (Path(root) / "active" / "bandi" / bando_id / version).exists():
            return {"ok": False, "status": "promotion_policy_denied", "reason": "active_already_exists"}
    return {"ok": True}


def _stale_reasons(row: dict[str, Any]) -> list[str]:
    scope = row["scope"]
    current = build_approval_scope(
        action=row["action"],
        bando_id=row["bando_id"],
        version=row["domain_version"],
        canary_plan=scope.get("canary_plan") or {},
        root=Path(scope.get("registry_path") or "domains/registry.json").parent,
    )
    checks = {
        "git_commit_changed": ("git_commit",),
        "domain_manifest_changed": ("domain_manifest_hash",),
        "rule_set_changed": ("rule_set_hash",),
        "source_set_changed": ("source_set_hash",),
        "test_evidence_changed": ("test_evidence_hash",),
        "canary_plan_changed": ("canary_plan_hash",),
        "readiness_changed": ("promotion_readiness_hash",),
    }
    reasons = []
    for reason, keys in checks.items():
        for key in keys:
            if str(scope.get(key) or "") != str(current.get(key) or ""):
                reasons.append(reason)
                break
    return reasons


def _execute_promote(row: dict[str, Any]) -> dict[str, Any]:
    scope = row["scope"]
    src = Path(scope["source_draft_path"])
    dest = Path(scope["destination_active_path"])
    if dest.exists():
        return {"status": "execution_failed", "reason": "active_exists"}
    tmp = dest.with_name(dest.name + f".approval-{os.getpid()}.tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    shutil.copytree(src, tmp)
    manifest = read_yaml(tmp / "domain.yaml")
    manifest["state"] = "active"
    write_yaml_atomic(tmp / "domain.yaml", manifest)
    write_text_atomic(tmp / "manifest.sha256", sha256_tree(tmp) + "\n")
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(tmp, dest)
    registry_path = Path(scope["registry_path"])
    registry = read_json(registry_path) or {"domains": []}
    registry["domains"] = [item for item in registry.get("domains", []) if not (item.get("domain_id") == f"bandi/{row['bando_id']}" and item.get("version") == row["domain_version"] and item.get("state") == "active")]
    registry["domains"].append({"domain_id": f"bandi/{row['bando_id']}", "version": row["domain_version"], "state": "active", "path": str(dest)})
    write_json_atomic(registry_path, registry)
    return {"status": "executed", "action": "promote_domain", "active_path": str(dest), "active_hash": sha256_tree(dest)}


def _write_outbox(request: dict[str, Any], policy: DomainApprovalPolicy) -> dict[str, Any]:
    path = Path(os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX", "logs/domain_approval_outbox.jsonl"))
    record = {
        "status": "queued",
        "request_id": request["request_id"],
        "api_url": policy.api_url,
        "message": request.get("telegram_message"),
    }
    from .storage import append_jsonl

    append_jsonl(path, record)
    return {"status": "queued", "outbox": str(path)}


def _read_optional_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return {}


def _git_commit() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        return ""
