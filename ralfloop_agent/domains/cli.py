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
from .bando_domain_builder import BandoBuildRequest, BandoDomainBuilder
from .bando_source_review import assess_jury_sample, build_jury_sample, build_source_review
from .bando_web_research import BandoWebResearcher, WebResearchRequest
from .bandi_registry import BandoRegistry
from .builder import DomainBuilder
from .calculation_orchestrator import CalculationOrchestrator
from .capability_registry import CanonicalCapabilityRegistry
from .domain_approval_executor import approval_status, cancel_approval, execute_approved, request_domain_approval
from .domain_opinion import DomainReasoningInput, execute_domain_opinion
from .deterministic_engine import DeterministicEngine
from .existing_capability_importer import ExistingCapabilityImporter
from .promotion import DomainPromotionService
from .registry import DomainRegistry
from .reasoning_router import DomainReasoningRouter
from .resolver import DomainResolver
from .validator import DomainValidator
from .storage import append_jsonl, now_iso


def answer_goal(
    goal: str,
    domain_id: str | None = None,
    registry: DomainRegistry | None = None,
    jury_controller: Any | None = None,
    *,
    explicit_recursive: bool = False,
    prefer_hybrid: bool = False,
    requested_reason_codes: list[str] | None = None,
) -> dict[str, Any]:
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
    route = DomainReasoningRouter().select(
        domain_resolution=resolution,
        classification=classification,
        deterministic_result=deterministic,
        requested_reason_codes=requested_reason_codes,
        explicit_recursive=explicit_recursive,
        prefer_hybrid=prefer_hybrid,
    )
    if not route["use_recursive"]:
        return {
            "status": "domain_reasoning_required" if route["selected_backend"] == "single_qwen_7b_with_domain" else "incomplete",
            "domain": domain["manifest"],
            "classification": classification,
            "deterministic_result": deterministic,
            "routing": route,
            "jury_required": False,
            "external_action_executed": False,
        }
    controller = jury_controller or RecursiveMASRuntimeController.from_env()
    if not controller.config.enabled:
        return {"status": "jury_required", "classification": classification, "jury_required": True, "jury_status": "disabled", "jury_reason_codes": route["reason_codes"], "routing": route, "deterministic_result": deterministic, "answer": None, "external_action_executed": False}
    reasoning_input = DomainReasoningInput.from_domain(
        domain,
        goal,
        facts=deterministic.get("facts", {}),
        reason_codes=route["reason_codes"],
        constraints=["Preserve deterministic conclusions."],
    )
    result = execute_domain_opinion(reasoning_input, controller.execute)
    if not result.get("ok"):
        return {
            "status": result.get("status") or "recursive_output_invalid",
            "domain": {"domain_id": domain["manifest"]["domain_id"], "version": domain["manifest"]["version"]},
            "classification": classification,
            "resolution_type": "jury",
            "deterministic_result": deterministic,
            "jury_result": result,
            "answer": None,
            "external_action_executed": False,
        }
    answer = result["opinion"]
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
        "jury_interpretations": [answer["position"]],
        "hypotheses": [],
        "recommendations": [answer["recommendation"]] if "recommendation_required" in route["reason_codes"] else [],
        "unknowns": answer["uncertainties"],
        "confidence": answer["confidence"],
        "evidence_refs": deterministic.get("evidence_refs", []),
        "conflicts": deterministic.get("conflicts", []),
        "limitations": ["jury_interpretation_not_fact"],
        "external_action_executed": False,
    }


_answer_goal_impl = answer_goal


def answer_goal(
    goal: str,
    domain_id: str | None = None,
    registry: DomainRegistry | None = None,
    jury_controller: Any | None = None,
    *,
    explicit_recursive: bool = False,
    prefer_hybrid: bool = False,
    requested_reason_codes: list[str] | None = None,
) -> dict[str, Any]:
    result = _answer_goal_impl(
        goal,
        domain_id,
        registry,
        jury_controller,
        explicit_recursive=explicit_recursive,
        prefer_hybrid=prefer_hybrid,
        requested_reason_codes=requested_reason_codes,
    )
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
    if status == "capability_missing":
        return 3
    if status == "parity_failed":
        return 4
    if status == "mapping_conflict":
        return 5
    if status == "source_invalid":
        return 6
    if status == "validation_failed" or out.get("valid") is False or out.get("ok") is False:
        return 1
    return 0


def _capabilities_command(args: argparse.Namespace) -> dict[str, Any]:
    registry = CanonicalCapabilityRegistry.from_mapping()
    importer = ExistingCapabilityImporter(capability_registry=registry)
    try:
        if args.capability_cmd == "list":
            return {"status": "ok", "capabilities": registry.list_capabilities()}
        if args.capability_cmd == "show":
            return {"status": "ok", "capability": registry.get(args.capability).to_dict()}
        if args.capability_cmd == "audit":
            mapping = registry.validate_mapping()
            sources = registry.verify_sources()
            status = "ok"
            if not mapping["ok"]:
                status = "mapping_conflict"
            elif not sources["ok"]:
                status = "source_invalid"
            return {"status": status, "mapping": mapping, "sources": sources}
        if args.capability_cmd == "parity":
            result = registry.parity(args.capability)
            if not result.get("ok"):
                result["status"] = "parity_failed"
            return result
        if args.capability_cmd == "import-draft":
            cap = registry.get(args.capability)
            return importer.create_domain_draft(cap.domain_id)
        if args.capability_cmd == "import-domain":
            return importer.create_domain_draft(args.domain)
    except KeyError as exc:
        return {"status": "capability_missing", "error": str(exc)}
    return {"status": "input_invalid", "error": "unknown_capabilities_command"}


def _bandi_command(args: argparse.Namespace) -> dict[str, Any]:
    registry = BandoRegistry()
    if args.bandi_cmd == "list":
        return {"status": "ok", "bandi": [item.to_dict() for item in registry._load_versions()]}
    if args.bandi_cmd == "show":
        version = registry.get_version(args.bando, args.version)
        return {"status": "ok", "bando": version.to_dict()} if version else {"status": "missing_context", "bando_id": args.bando}
    if args.bandi_cmd == "validate":
        version = registry.get_version(args.bando, args.version)
        if not version:
            return {"status": "missing_context", "bando_id": args.bando}
        source_report = registry.verify_sources(args.bando, args.version)
        conflicts = registry.detect_conflicts(args.bando, args.version)
        return {
            "status": "validation_required" if conflicts or not source_report["ok"] else "draft_validatable",
            "bando_id": args.bando,
            "version": version.version,
            "source_results": source_report,
            "conflicts": [item.to_dict() for item in conflicts],
            "state": "draft",
        }
    if args.bandi_cmd == "review":
        return registry.review(args.bando, args.version)
    if args.bandi_cmd == "inspect-sources":
        builder = BandoDomainBuilder()
        request = BandoBuildRequest(
            primary_document=args.source[0] if args.source else None,
            supporting_documents=args.source[1:],
        )
        return {"status": "ok", "sources": [item.to_dict() for item in builder.inspect_sources(request)]}
    if args.bandi_cmd == "build":
        builder = BandoDomainBuilder(args.output_root)
        return builder.build_draft(
            BandoBuildRequest(
                primary_document=args.primary_document,
                attachments=args.attachment,
                faq_documents=args.faq,
                amendments=args.amendment,
                referenced_regulations=args.referenced_regulation,
                operational_manuals=args.operational_manual,
                supporting_documents=args.supporting_document,
                output_root=args.output_root,
            )
        ).to_dict()
    if args.bandi_cmd == "compare-versions":
        return BandoDomainBuilder().compare_versions(args.bando, args.from_version, args.to_version)
    if args.bandi_cmd == "research":
        return BandoDomainBuilder().research_web(args.bando, args.version, allow_web=args.allow_web, offline=args.offline)
    if args.bandi_cmd == "research-source":
        request = WebResearchRequest.from_env(
            issuer=args.issuer,
            queries=[args.query],
            allow_web=args.allow_web,
            offline=args.offline,
        )
        return BandoWebResearcher().research(request).to_dict()
    if args.bandi_cmd == "complete-domain":
        builder = BandoDomainBuilder(args.output_root)
        local = builder.build_draft(BandoBuildRequest(primary_document=args.primary_document, output_root=args.output_root)).to_dict()
        if not local.get("bando_id"):
            return local
        research = builder.research_web(local["bando_id"], local["version"], allow_web=args.allow_web, offline=args.offline)
        return {"status": "domain_completion_review", "local_build": local, "research": research, "active_modified": False}
    if args.bandi_cmd == "check-updates":
        out = BandoDomainBuilder().research_web(args.bando, args.version, allow_web=args.allow_web, offline=args.offline)
        if out.get("accepted_sources"):
            out["status"] = "validation_required"
        return out
    if args.bandi_cmd == "source-review":
        research = json.loads(Path(args.research_result).read_text(encoding="utf-8"))
        return build_source_review(
            bando_id=args.bando,
            version=args.version,
            research_result=research,
            output_dir=args.output_dir,
        ).to_dict()
    if args.bandi_cmd == "jury-sources":
        raw = json.loads(Path(args.candidates).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "accepted_sources" in raw:
            candidates = build_jury_sample(raw, max_candidates=args.max_candidates)
            issuer = raw.get("issuer") or _issuer_for_bando(args.bando)
        elif isinstance(raw, dict) and "sources" in raw:
            candidates = raw["sources"]
            issuer = raw.get("issuer") or _issuer_for_bando(args.bando)
        elif isinstance(raw, list):
            candidates = raw
            issuer = _issuer_for_bando(args.bando)
        else:
            return {"status": "input_invalid", "error": "unsupported_candidates_file"}
        return assess_jury_sample(
            bando_id=args.bando,
            version=args.version,
            issuer=issuer,
            candidates=candidates,
            recursive_mas=args.recursive_mas,
            max_candidates=args.max_candidates,
            vram_aware=args.vram_aware,
            batch_size=args.batch_size,
            oom_backoff=args.oom_backoff,
            audit_dir=args.audit_dir,
        )
    if args.bandi_cmd == "request-approval":
        canary = json.loads(Path(args.canary_plan).read_text(encoding="utf-8")) if args.canary_plan else None
        return request_domain_approval(
            action=args.action,
            bando_id=args.bando,
            version=args.version,
            canary_plan=canary,
            send_telegram=args.send_telegram,
        )
    if args.bandi_cmd == "approval-status":
        return approval_status(args.request_id)
    if args.bandi_cmd == "execute-approved":
        return execute_approved(args.request_id, dry_run=args.dry_run)
    if args.bandi_cmd == "cancel-approval":
        return cancel_approval(args.request_id)
    if args.bandi_cmd == "evaluate":
        out = registry.evaluate({"bando_id": args.bando, "version": args.version, "goal": args.goal}).to_dict()
        out["deterministic_complete"] = out.get("status") == "completed" and bool(out.get("deterministic"))
        if out.get("jury_required") and os.getenv("RALFLOOP_ENABLE_DOMAIN_JURY") != "1":
            out["jury_status"] = "disabled"
        return out
    return {"status": "input_invalid", "error": "unknown_bandi_command"}


def _issuer_for_bando(bando_id: str) -> str:
    if "unipolis" in bando_id:
        return "Fondazione Unipolis"
    if "cariplo" in bando_id:
        return "Fondazione Cariplo"
    return ""


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
    calc = sub.add_parser("calculate"); calc.add_argument("--expression"); calc.add_argument("--goal")
    caps = sub.add_parser("capabilities")
    caps_sub = caps.add_subparsers(dest="capability_cmd", required=True, parser_class=JsonArgumentParser)
    caps_sub.add_parser("list")
    cs = caps_sub.add_parser("show"); cs.add_argument("--capability", required=True)
    caps_sub.add_parser("audit")
    cp = caps_sub.add_parser("parity"); cp.add_argument("--capability", required=True)
    ci = caps_sub.add_parser("import-draft"); ci.add_argument("--capability", required=True)
    cd = caps_sub.add_parser("import-domain"); cd.add_argument("--domain", required=True)
    bandi = sub.add_parser("bandi")
    bandi_sub = bandi.add_subparsers(dest="bandi_cmd", required=True, parser_class=JsonArgumentParser)
    bandi_sub.add_parser("list")
    bs = bandi_sub.add_parser("show"); bs.add_argument("--bando", required=True); bs.add_argument("--version")
    bv = bandi_sub.add_parser("validate"); bv.add_argument("--bando", required=True); bv.add_argument("--version")
    br = bandi_sub.add_parser("review"); br.add_argument("--bando", required=True); br.add_argument("--version")
    bi = bandi_sub.add_parser("inspect-sources"); bi.add_argument("--source", action="append", required=True)
    bb = bandi_sub.add_parser("build")
    bb.add_argument("--primary-document", required=True)
    bb.add_argument("--attachment", action="append", default=[])
    bb.add_argument("--faq", action="append", default=[])
    bb.add_argument("--amendment", action="append", default=[])
    bb.add_argument("--referenced-regulation", action="append", default=[])
    bb.add_argument("--operational-manual", action="append", default=[])
    bb.add_argument("--supporting-document", action="append", default=[])
    bb.add_argument("--output-root", default="domains")
    bc = bandi_sub.add_parser("compare-versions")
    bc.add_argument("--bando", required=True)
    bc.add_argument("--from", dest="from_version", required=True)
    bc.add_argument("--to", dest="to_version", required=True)
    bre = bandi_sub.add_parser("research")
    bre.add_argument("--bando", required=True)
    bre.add_argument("--version", required=True)
    bre.add_argument("--allow-web", action="store_true")
    bre.add_argument("--offline", action="store_true")
    brs = bandi_sub.add_parser("research-source")
    brs.add_argument("--query", required=True)
    brs.add_argument("--issuer", default="")
    brs.add_argument("--allow-web", action="store_true")
    brs.add_argument("--offline", action="store_true")
    bcd = bandi_sub.add_parser("complete-domain")
    bcd.add_argument("--primary-document", required=True)
    bcd.add_argument("--output-root", default="domains")
    bcd.add_argument("--allow-web", action="store_true")
    bcd.add_argument("--offline", action="store_true")
    bcu = bandi_sub.add_parser("check-updates")
    bcu.add_argument("--bando", required=True)
    bcu.add_argument("--version", required=True)
    bcu.add_argument("--allow-web", action="store_true")
    bcu.add_argument("--offline", action="store_true")
    bsr = bandi_sub.add_parser("source-review")
    bsr.add_argument("--bando", required=True)
    bsr.add_argument("--version", required=True)
    bsr.add_argument("--research-result", required=True)
    bsr.add_argument("--output-dir")
    bjs = bandi_sub.add_parser("jury-sources")
    bjs.add_argument("--bando", required=True)
    bjs.add_argument("--version", required=True)
    bjs.add_argument("--candidates", required=True)
    bjs.add_argument("--max-candidates", type=int, default=8)
    bjs.add_argument("--recursive-mas", action="store_true")
    bjs.add_argument("--vram-aware", action="store_true")
    bjs.add_argument("--batch-size", default="auto")
    bjs.add_argument("--oom-backoff", dest="oom_backoff", action="store_true", default=True)
    bjs.add_argument("--no-oom-backoff", dest="oom_backoff", action="store_false")
    bjs.add_argument("--audit-dir")
    bra = bandi_sub.add_parser("request-approval")
    bra.add_argument("--action", required=True, choices=["promote_domain", "run_domain_canary", "apply_domain_source_update"])
    bra.add_argument("--bando", required=True)
    bra.add_argument("--version", required=True)
    bra.add_argument("--canary-plan")
    bra.add_argument("--send-telegram", action="store_true")
    bas = bandi_sub.add_parser("approval-status")
    bas.add_argument("--request-id", required=True)
    bea = bandi_sub.add_parser("execute-approved")
    bea.add_argument("--request-id", required=True)
    bea.add_argument("--dry-run", action="store_true")
    bca = bandi_sub.add_parser("cancel-approval")
    bca.add_argument("--request-id", required=True)
    be = bandi_sub.add_parser("evaluate"); be.add_argument("--bando", required=True); be.add_argument("--version"); be.add_argument("--goal", required=True)
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
        elif args.cmd == "calculate": out = CalculationOrchestrator().calculate({"expression": args.expression, "goal": args.goal})
        elif args.cmd == "capabilities": out = _capabilities_command(args)
        elif args.cmd == "bandi": out = _bandi_command(args)
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
