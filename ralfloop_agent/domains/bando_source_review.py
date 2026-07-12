from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .bando_source_jury_scheduler import SourceJuryBatchScheduler, SourceJuryVramPolicy
from .source_authority import SourceCandidate, assess_authority, infer_official_domains
from .source_jury import SourceJury


@dataclass
class SourceReviewSummary:
    status: str
    bando_id: str
    version: str
    source_count: int = 0
    binding_count: int = 0
    supporting_count: int = 0
    discovery_only_count: int = 0
    rejected_count: int = 0
    not_fetched_count: int = 0
    duplicate_count: int = 0
    fetch_failed_count: int = 0
    human_review_required: bool = False
    output_dir: str | None = None
    source_update_proposal: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_source_review(
    *,
    bando_id: str,
    version: str,
    research_result: dict[str, Any],
    output_dir: str | Path | None = None,
) -> SourceReviewSummary:
    accepted = list(research_result.get("accepted_sources") or [])
    rejected = list(research_result.get("rejected_sources") or [])
    binding = [item for item in accepted if item.get("jury", {}).get("recommended_use") == "binding"]
    supporting = [item for item in accepted if item.get("jury", {}).get("recommended_use") == "supporting"]
    discovery = [item for item in rejected if item.get("jury", {}).get("recommended_use") == "discovery_only"]
    rejected_only = [item for item in rejected if item.get("jury", {}).get("recommended_use") == "reject"]
    proposal = {
        "new_official_sources": [_brief_source(item) for item in binding],
        "identical_sources": [],
        "possible_updates": [_brief_source(item) for item in binding],
        "possible_rule_changes": [],
        "conflicts": research_result.get("conflicts", []),
        "missing_documents": _missing_documents(research_result),
        "recommended_next_action": "human_review_required" if binding or supporting else "continue_research",
        "domain_promoted": False,
        "active_modified": False,
    }
    summary = SourceReviewSummary(
        "source_review_ready",
        bando_id,
        version,
        source_count=len(research_result.get("candidates") or []),
        binding_count=len(binding),
        supporting_count=len(supporting),
        discovery_only_count=len(discovery),
        rejected_count=len(rejected_only),
        not_fetched_count=int(research_result.get("not_fetched_count") or 0),
        duplicate_count=int(research_result.get("duplicate_count") or 0),
        fetch_failed_count=int(research_result.get("fetch_failed_count") or 0),
        human_review_required=bool(research_result.get("human_review_required") or binding or supporting),
        output_dir=str(output_dir) if output_dir else None,
        source_update_proposal=proposal,
    )
    if output_dir:
        _write_review_files(Path(output_dir), research_result, binding, supporting, discovery, rejected_only, proposal, summary)
    return summary


def build_jury_sample(research_result: dict[str, Any], *, max_candidates: int = 8) -> list[dict[str, Any]]:
    accepted = list(research_result.get("accepted_sources") or [])
    rejected = list(research_result.get("rejected_sources") or [])
    binding = [item for item in accepted if item.get("jury", {}).get("recommended_use") == "binding"]
    supporting = [item for item in accepted if item.get("jury", {}).get("recommended_use") == "supporting"]
    failed = [item for item in rejected if item.get("authority", {}).get("deterministic_gate") is False]
    sample: list[dict[str, Any]] = []
    for group in (binding[:2], supporting[:2], failed[:2], rejected[:2], accepted[2:]):
        for item in group:
            key = item.get("candidate_id") or item.get("url")
            if key and all((row.get("candidate_id") or row.get("url")) != key for row in sample):
                sample.append(item)
            if len(sample) >= max_candidates:
                return sample
    return sample[:max_candidates]


def assess_jury_sample(
    *,
    bando_id: str,
    version: str,
    issuer: str,
    candidates: list[dict[str, Any]],
    recursive_mas: bool = False,
    max_candidates: int = 8,
    vram_aware: bool = False,
    batch_size: int | str = "auto",
    oom_backoff: bool = True,
    audit_dir: str | Path | None = None,
) -> dict[str, Any]:
    candidates = candidates[:max_candidates]
    if recursive_mas and vram_aware:
        policy = SourceJuryVramPolicy.from_env()
        policy.max_candidates = max_candidates
        policy.batch_size = 0 if batch_size == "auto" else int(batch_size)
        policy.oom_backoff = oom_backoff
        scheduler = SourceJuryBatchScheduler(policy)

        def _runner(batch: list[dict[str, Any]], _batch_id: str) -> dict[str, Any]:
            return assess_jury_sample(
                bando_id=bando_id,
                version=version,
                issuer=issuer,
                candidates=batch,
                recursive_mas=True,
                max_candidates=len(batch),
                vram_aware=False,
            )

        aggregate = scheduler.run(candidates, runner=_runner, audit_dir=audit_dir).to_dict()
        deterministic = [
            {
                "candidate_id": item["candidate_id"],
                "deterministic_assessment": item["deterministic_assessment"],
                "jury_assessment": item["jury_assessment"],
                "final_assessment": item["final_assessment"],
                "human_review_required": item["human_review_required"],
            }
            for item in aggregate["candidates"]
        ]
        return {
            "status": "jury_sources_completed" if not aggregate["partial"] else "jury_sources_partial",
            "bando_id": bando_id,
            "version": version,
            "candidate_count": aggregate["candidate_count"],
            "completed_count": aggregate["completed_count"],
            "failed_count": aggregate["failed_count"],
            "jury_backend": "recursive_mas_native",
            "recursive_mas_requested": True,
            "vram_aware": True,
            "native_latent_verified": aggregate["native_latent_verified"],
            "native_latent_verified_all_batches": aggregate["native_latent_verified_all_batches"],
            "fallback": aggregate["fallback"],
            "rounds": aggregate["rounds"],
            "deterministic_results": deterministic,
            "native_result": aggregate,
            "batch_plan": aggregate["batch_plan"],
            "batches": aggregate["batches"],
            "hard_gate_overridden": False,
        }
    deterministic = []
    official_domains = infer_official_domains(issuer)
    jury = SourceJury()
    for row in candidates:
        candidate = _candidate_from_row(row, issuer)
        authority = assess_authority(candidate, issuer=issuer, official_domains=official_domains)
        shim = jury.assess(candidate, authority)
        final = shim.to_dict()
        if not authority.deterministic_gate:
            final["recommended_use"] = "reject" if authority.authority_level == "D" else "discovery_only"
            final["binding_eligible"] = False
            final.setdefault("concerns", []).append("hard_gate_final_override")
        deterministic.append(
            {
                "candidate_id": candidate.candidate_id,
                "url": candidate.url,
                "title": candidate.title,
                "deterministic_assessment": authority.to_dict(),
                "jury_assessment": shim.to_dict(),
                "final_assessment": final,
                "agreement": authority.authority_level == final.get("authority_level"),
                "disagreement": authority.authority_level != final.get("authority_level"),
                "human_review_required": bool(final.get("human_review_required") or authority.authority_level != final.get("authority_level")),
            }
        )
    native = _run_recursive_mas_source_jury(bando_id, version, issuer, deterministic) if recursive_mas else {"status": "not_requested", "jury_backend": "deterministic_shim"}
    return {
        "status": "jury_sources_completed",
        "bando_id": bando_id,
        "version": version,
        "candidate_count": len(candidates),
        "jury_backend": native.get("jury_backend", "deterministic_shim"),
        "recursive_mas_requested": recursive_mas,
        "native_latent_verified": bool(native.get("native_latent_verified")),
        "fallback": bool(native.get("fallback_used")),
        "rounds": native.get("rounds", 0),
        "deterministic_results": deterministic,
        "native_result": native,
        "hard_gate_overridden": False,
    }


def _run_recursive_mas_source_jury(bando_id: str, version: str, issuer: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    from ralfloop_agent.integration.recursive_mas_runtime import RecursiveMASRuntimeController

    controller = RecursiveMASRuntimeController.from_env()
    if not controller.config.enabled:
        return {"status": "disabled", "jury_backend": "recursive_mas_native", "native_latent_verified": False, "fallback_used": False, "rounds": 1}
    prompt = {
        "task": "source_authority_review",
        "bando_id": bando_id,
        "version": version,
        "issuer": issuer,
        "roles": [
            "issuer_identity_reviewer",
            "official_source_verifier",
            "document_version_reviewer",
            "rule_provenance_reviewer",
            "adversarial_source_reviewer",
            "final_source_synthesizer",
        ],
        "constraints": [
            "do_not_override_hard_gate",
            "do_not_make_failed_gate_binding",
            "do_not_use_jury_output_as_source_ref",
        ],
        "candidates": rows,
    }
    result = controller.execute({"goal": json.dumps(prompt, ensure_ascii=False), "rounds": 1, "fallback": False, "profile": "deterministic_diagnostic"})
    return {
        "status": result.get("status"),
        "ok": result.get("ok"),
        "jury_backend": result.get("selected_backend", "recursive_mas_native"),
        "native_latent_verified": bool(result.get("native_latent_verified")),
        "fallback_used": bool(result.get("fallback_used")),
        "rounds": 1,
        "answer": result.get("answer"),
        "cleanup_completed": result.get("cleanup_completed"),
        "raw": result,
    }


def _write_review_files(
    output_dir: Path,
    research: dict[str, Any],
    binding: list[dict[str, Any]],
    supporting: list[dict[str, Any]],
    discovery: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    proposal: dict[str, Any],
    summary: SourceReviewSummary,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "source-inventory.json": research.get("candidates", []),
        "source-ranking.json": sorted(research.get("candidates", []), key=lambda item: item.get("metadata", {}).get("preliminary_rank_score", 0), reverse=True),
        "source-deduplication.json": {"duplicate_urls": research.get("duplicate_urls", []), "duplicate_content": research.get("duplicate_content", [])},
        "binding-sources.json": binding,
        "supporting-sources.json": supporting,
        "discovery-only.json": discovery,
        "rejected-sources.json": rejected,
        "jury-assessments.json": research.get("jury_assessments", []),
        "conflicts.json": research.get("conflicts", []),
        "missing-documents.json": _missing_documents(research),
        "source_update_proposal.json": proposal,
        "summary.json": summary.to_dict(),
    }
    for name, payload in files.items():
        (output_dir / name).write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    checksums = []
    for item in research.get("downloads", []):
        checksum = item.get("checksum")
        cache_path = item.get("cache_path")
        if checksum and cache_path:
            checksums.append(f"{checksum}  {cache_path}")
    (output_dir / "checksums.sha256").write_text("\n".join(checksums) + ("\n" if checksums else ""), encoding="utf-8")
    (output_dir / "human-review.md").write_text(
        "\n".join(
            [
                f"# Source Review {research.get('bando_id', '')} {research.get('version', '')}",
                "",
                f"- binding: {len(binding)}",
                f"- supporting: {len(supporting)}",
                f"- discovery_only: {len(discovery)}",
                f"- rejected: {len(rejected)}",
                "- promotion: not allowed",
                "- domain_update: proposal only",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _missing_documents(research: dict[str, Any]) -> list[dict[str, Any]]:
    found = " ".join(json.dumps(item, ensure_ascii=False).lower() for item in research.get("candidates", []))
    wanted = {
        "official_faq": "faq",
        "official_amendment": "rettifica",
        "official_deadline_extension": "proroga",
        "official_reporting_manual": "rendicont",
        "official_post_selection_agreement": "accordo",
    }
    return [{"document_type": key, "status": "not_found" if token not in found else "candidate_found"} for key, token in wanted.items()]


def _brief_source(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": item.get("candidate_id"),
        "title": item.get("title"),
        "url": item.get("url"),
        "checksum": (item.get("fetch") or {}).get("checksum") or item.get("checksum"),
        "authority_level": (item.get("jury") or {}).get("authority_level"),
        "recommended_use": (item.get("jury") or {}).get("recommended_use"),
    }


def _candidate_from_row(row: dict[str, Any], issuer: str) -> SourceCandidate:
    authority = row.get("authority") or {}
    fetch = row.get("fetch") or {}
    return SourceCandidate(
        candidate_id=str(row.get("candidate_id") or row.get("id") or row.get("url")),
        url=str(row.get("url") or fetch.get("final_url") or ""),
        title=str(row.get("title") or ""),
        publisher=str(row.get("publisher") or authority.get("publisher") or ""),
        issuer=issuer,
        document_type=str(row.get("document_type") or authority.get("document_type") or "unknown"),
        content_type=str(fetch.get("content_type") or row.get("content_type") or authority.get("content_type") or ""),
        checksum=str(fetch.get("checksum") or row.get("checksum") or authority.get("checksum") or ""),
        publication_date=row.get("publication_date") or authority.get("publication_date"),
        version=row.get("version") or authority.get("version"),
        snippet=str(row.get("snippet") or ""),
        direct_document=bool(row.get("direct_document") or authority.get("direct_document")),
        metadata=dict(row.get("metadata") or {}),
    )
