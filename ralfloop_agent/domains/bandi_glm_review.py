from __future__ import annotations

from typing import Any, Iterable

from .bandi_semantic_retrieval import EvidenceEnvelope, retrieve_for_bandi
from ralfloop_agent.glm_review.service import GlmReviewService


def enqueue_grant_review(
    *,
    task_id: str,
    draft: str,
    requirements: list[str],
    evidence: Iterable[EvidenceEnvelope],
    goal: str,
    budget_summary: dict[str, Any] | None = None,
    deadline: str | None = None,
    constraints: list[str] | None = None,
    known_gaps: list[str] | None = None,
    external_action: str | None = None,
    semantic_invoke: Any | None = None,
    service: GlmReviewService | None = None,
    immediate: bool = False,
) -> dict[str, Any]:
    """Bridge from the real bandi evidence/retrieval contract into slow review."""
    ranked = retrieve_for_bandi(
        " ".join([goal, *requirements]),
        evidence,
        semantic_invoke=semantic_invoke,
        top_k=8,
    )
    payload = {
        "goal": goal,
        "constraints": constraints or [],
        "draft": draft,
        "requirements": requirements,
        "evidence": [
            {
                "source_id": item.chunk_id,
                "location": item.canonical_url,
                "claim": item.section or item.anchor or "retrieved source evidence",
                "excerpt": item.text,
                "classification": "fact",
            }
            for item in ranked.evidence
        ],
        "budget_summary": budget_summary or {},
        "known_gaps": known_gaps or [],
        "requested_review": [
            "mandatory requirements",
            "objectives activities KPI coherence",
            "budget coherence",
            "contradictions",
            "missing evidence",
        ],
        "deadline": deadline,
        "external_action": external_action,
        "retrieval": ranked.to_dict(),
    }
    return (service or GlmReviewService()).enqueue_payload(
        payload,
        task_type="grant_review",
        task_id=task_id,
        immediate=immediate,
        external_action=external_action,
        source_path="bandi_pipeline",
    )
