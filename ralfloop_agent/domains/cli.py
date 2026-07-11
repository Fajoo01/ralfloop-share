from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from ralfloop_agent.integration.recursive_mas_runtime import RecursiveMASRuntimeController
from .builder import DomainBuilder
from .deterministic_engine import DeterministicEngine
from .jury_router import DomainJuryRouter
from .promotion import DomainPromotionService
from .registry import DomainRegistry
from .resolver import DomainResolver
from .validator import DomainValidator
from .storage import append_jsonl, now_iso


def answer_goal(goal: str, domain_id: str | None = None, registry: DomainRegistry | None = None, jury_controller: Any | None = None) -> dict[str, Any]:
    registry = registry or DomainRegistry()
    resolver = DomainResolver(registry)
    resolution = resolver.resolve(goal, {"domain_id": domain_id} if domain_id else None).to_dict()
    if resolution["status"] == "missing":
        return {"status": "domain_creation_required", "domain_resolution": resolution, "external_action_executed": False}
    if resolution["status"] == "ambiguous":
        return {"status": "ambiguous", "domain_resolution": resolution, "jury_required": True, "external_action_executed": False}
    domain = registry.find_active(resolution["domain_id"])
    if not domain or domain["manifest"].get("state") != "active":
        return {"status": "domain_not_active", "domain_resolution": resolution, "external_action_executed": False}
    integrity = registry.verify_integrity(domain["manifest"]["domain_id"], domain["manifest"]["version"])
    if not integrity.get("ok"):
        return {"status": "quarantined", "domain_resolution": resolution, "integrity": integrity, "external_action_executed": False}
    engine = DeterministicEngine()
    classification = engine.classify(domain, goal)
    deterministic = engine.evaluate(domain, goal)
    if classification == "external_action":
        return {
            "status": "human_confirmation_required",
            "domain": {"domain_id": domain["manifest"]["domain_id"], "version": domain["manifest"]["version"]},
            "classification": classification,
            "deterministic_result": deterministic,
            "jury_required": False,
            "external_action_executed": False,
            "human_confirmation": {"required": True, "reason": "external_action"},
        }
    if deterministic.get("complete"):
        return {"status": "completed", "domain": {"domain_id": domain["manifest"]["domain_id"], "version": domain["manifest"]["version"]}, "classification": classification, "resolution_type": "deterministic", "answer": deterministic["result"], "deterministic_result": deterministic, "jury_invoked": False, "confidence": deterministic["confidence"], "evidence_refs": deterministic["evidence_refs"], "conflicts": [], "limitations": [], "external_action_executed": False}
    jury = DomainJuryRouter().should_use_jury(domain_resolution=resolution, classification=classification, deterministic_result=deterministic)
    if not jury["use_jury"]:
        return {"status": "incomplete", "domain": domain["manifest"], "deterministic_result": deterministic, "jury_required": False, "external_action_executed": False}
    controller = jury_controller or RecursiveMASRuntimeController.from_env()
    if not controller.config.enabled:
        return {"status": "jury_required", "classification": classification, "jury_required": True, "jury_status": "disabled", "jury_reason_codes": jury["reason_codes"], "deterministic_result": deterministic, "answer": None, "external_action_executed": False}
    prompt = _compact_context(goal, domain, deterministic, jury)
    result = controller.execute({"goal": prompt, "rounds": jury["rounds"], "profile": "deterministic_diagnostic"})
    answer = result.get("answer")
    return {
        "status": "completed" if result.get("ok") else "jury_failed",
        "domain": {"domain_id": domain["manifest"]["domain_id"], "version": domain["manifest"]["version"]},
        "classification": classification,
        "resolution_type": "jury",
        "deterministic_result": deterministic,
        "jury_result": result,
        "answer": answer,
        "facts": deterministic.get("facts", {}),
        "deterministic_conclusions": deterministic.get("derived_values", {}),
        "jury_interpretations": [answer] if result.get("ok") and answer else [],
        "hypotheses": [],
        "recommendations": [answer] if "recommendation_required" in jury["reason_codes"] and answer else [],
        "unknowns": deterministic.get("unresolved_questions", []),
        "confidence": 0.7 if result.get("ok") else 0.0,
        "evidence_refs": deterministic.get("evidence_refs", []),
        "conflicts": deterministic.get("conflicts", []),
        "limitations": ["jury_interpretation_not_fact"],
        "external_action_executed": False,
    }


_answer_goal_impl = answer_goal


def answer_goal(goal: str, domain_id: str | None = None, registry: DomainRegistry | None = None, jury_controller: Any | None = None) -> dict[str, Any]:
    result = _answer_goal_impl(goal, domain_id, registry, jury_controller)
    _audit_domain_answer(goal, result)
    return result


def _audit_domain_answer(goal: str, result: dict[str, Any]) -> None:
    try:
        resolution = result.get("domain_resolution") or {}
        deterministic = result.get("deterministic_result") or {}
        jury_result = result.get("jury_result") or {}
        record = {
            "timestamp": now_iso(),
            "request_id": str(uuid.uuid4()),
            "goal_hash": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
            "domain_resolution": resolution.get("status") or result.get("status"),
            "domain_candidates": resolution.get("candidates", []),
            "classification": result.get("resolution_type") or result.get("status"),
            "deterministic_rules_used": deterministic.get("matched_rules", []),
            "deterministic_complete": bool(deterministic.get("complete", False)),
            "jury_invoked": bool(jury_result),
            "jury_reason_codes": result.get("jury_reason_codes") or [],
            "jury_backend": jury_result.get("selected_backend") if isinstance(jury_result, dict) else None,
            "domain_creation_started": result.get("status") == "domain_creation_required",
            "draft_domain_id": None,
            "validation_result": None,
            "approval_required": result.get("status") == "approval_required",
            "promotion_result": None,
            "errors": result.get("blocking_errors") or result.get("errors") or [],
        }
        append_jsonl(Path("logs/domain_orchestrator.jsonl"), record)
    except Exception:
        pass


def _compact_context(goal: str, domain: dict[str, Any], deterministic: dict[str, Any], jury: dict[str, Any]) -> str:
    m = domain["manifest"]
    return json.dumps({"goal": goal, "manifest": {"domain_id": m["domain_id"], "version": m["version"], "scope": m.get("scope")}, "deterministic_result": deterministic, "jury_reason_codes": jury["reason_codes"]}, ensure_ascii=False)


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        _emit_json({"status": "input_invalid", "error": message})
        print(f"input_invalid: {message}", file=sys.stderr)
        raise SystemExit(2)


def _emit_json(out: dict[str, Any]) -> None:
    print(json.dumps(out, ensure_ascii=False, sort_keys=True))


def _exit_code_for(out: dict[str, Any]) -> int:
    status = out.get("status")
    if status == "input_invalid":
        return 2
    if status in {"missing", "domain_creation_required"}:
        return 3
    if bool(out.get("jury_required")) and out.get("jury_status") == "disabled":
        return 4
    if status == "approval_required":
        return 5
    if status == "validation_failed" or out.get("valid") is False or out.get("ok") is False:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = JsonArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True, parser_class=JsonArgumentParser)
    sub.add_parser("list")
    st = sub.add_parser("status"); st.add_argument("--domain", required=True)
    rs = sub.add_parser("resolve"); rs.add_argument("--goal", required=True)
    ans = sub.add_parser("answer"); ans.add_argument("--goal", required=True); ans.add_argument("--domain")
    cr = sub.add_parser("create-draft"); cr.add_argument("--goal", required=True); cr.add_argument("--source", action="append", default=[])
    va = sub.add_parser("validate"); va.add_argument("--domain", required=True); va.add_argument("--version", required=True)
    pr = sub.add_parser("promote"); pr.add_argument("--domain", required=True); pr.add_argument("--version", required=True); pr.add_argument("--approved-by", required=True); pr.add_argument("--approval-token-env", required=True); pr.add_argument("--approval-reason", default="")
    de = sub.add_parser("deprecate"); de.add_argument("--domain", required=True); de.add_argument("--version", required=True)
    try:
        args = parser.parse_args(argv)
        registry = DomainRegistry()
        if args.cmd == "list": out = {"domains": registry.list_domains()}
        elif args.cmd == "status": out = registry.get_domain(args.domain) or {"status": "missing"}
        elif args.cmd == "resolve": out = DomainResolver(registry).resolve(args.goal).to_dict()
        elif args.cmd == "answer": out = answer_goal(args.goal, args.domain, registry)
        elif args.cmd == "create-draft": out = DomainBuilder(registry).create_draft(args.goal, args.source, {}).to_dict()
        elif args.cmd == "validate":
            dom = registry.get_domain(args.domain, args.version); out = DomainValidator().validate(dom).to_dict() if dom else {"valid": False, "blocking_issues": ["domain_missing"]}
        elif args.cmd == "promote": out = DomainPromotionService(registry).promote(args.domain, args.version, approved_by=args.approved_by, approval_token=os.getenv(args.approval_token_env), approval_reason=args.approval_reason).to_dict()
        elif args.cmd == "deprecate": out = {"ok": registry.deprecate(args.domain, args.version)}
        else: raise AssertionError(args.cmd)
        _emit_json(out)
        return _exit_code_for(out)
    except SystemExit:
        raise
    except Exception as exc:
        out = {"status": "internal_error", "error": {"type": type(exc).__name__, "message": str(exc)}}
        _emit_json(out)
        print(f"internal_error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
