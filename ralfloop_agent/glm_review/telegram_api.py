from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request as StarletteRequest

from ralfloop_agent.domains.domain_approval import DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.domains.telegram_approval_api import verify_hmac_headers

from .service import GlmReviewService


def handle_glm_decision(
    *,
    task_id: str,
    body: bytes,
    headers: dict[str, str],
    path: str | None = None,
    policy: DomainApprovalPolicy | None = None,
    service: GlmReviewService | None = None,
    nonce_store: DomainApprovalStore | None = None,
) -> dict[str, Any]:
    policy = policy or DomainApprovalPolicy.from_env()
    if not policy.enabled:
        return {"status": "approval_gate_disabled", "task_id": task_id}
    nonce_store = nonce_store or DomainApprovalStore(policy=policy)
    auth = verify_hmac_headers(
        method="POST",
        path=path or f"/glm-reviews/{task_id}/decision",
        body=body,
        headers=headers,
        policy=policy,
        store=nonce_store,
    )
    if not auth["ok"]:
        return auth
    try:
        payload = json.loads(body.decode("utf-8"))
        user_id = int(payload.get("telegram_user_id") or 0)
        chat_id = int(payload.get("telegram_chat_id") or 0)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"status": "input_invalid", "task_id": task_id}
    if policy.allowed_user_ids and user_id not in policy.allowed_user_ids:
        return {"status": "unauthorized_user", "task_id": task_id}
    if policy.allowed_chat_ids and chat_id not in policy.allowed_chat_ids:
        return {"status": "unauthorized_chat", "task_id": task_id}
    if policy.require_private_chat and str(payload.get("chat_type") or "private") != "private":
        return {"status": "private_chat_required", "task_id": task_id}
    service = service or GlmReviewService()
    job = service.status(task_id)
    if job.get("status") == "not_found":
        return job
    if str(payload.get("artifact_hash") or "") != str(job.get("result_hash") or ""):
        return {"status": "artifact_hash_mismatch", "task_id": task_id}
    if int(payload.get("artifact_version") or 0) != int(job.get("artifact_version") or 0):
        return {"status": "artifact_version_mismatch", "task_id": task_id}
    decision = str(payload.get("decision") or "")
    actor = f"telegram-user:{user_id}"
    if decision == "approve":
        return service.approve(task_id, approver=actor, channel=f"telegram-chat:{chat_id}")
    if decision == "reject":
        return service.reject(
            task_id,
            approver=actor,
            channel=f"telegram-chat:{chat_id}",
            reason=str(payload.get("reason") or ""),
        )
    return {"status": "unsupported_decision", "task_id": task_id}


def register_glm_review_routes(app: Any) -> None:
    @app.get("/glm-reviews/health")
    async def glm_review_health() -> dict[str, Any]:
        service = GlmReviewService()
        return {"status": "ok", "provider": "colibri_glm", "metrics": service.queue.metrics()}

    @app.get("/glm-reviews/{task_id}")
    async def glm_review_status(task_id: str) -> dict[str, Any]:
        return GlmReviewService().status(task_id)

    @app.post("/glm-reviews/{task_id}/decision")
    async def glm_review_decision(task_id: str, request: StarletteRequest) -> dict[str, Any]:
        body = await request.body()
        return handle_glm_decision(
            task_id=task_id,
            body=body,
            headers={key: value for key, value in request.headers.items()},
            path=f"/glm-reviews/{task_id}/decision",
        )
