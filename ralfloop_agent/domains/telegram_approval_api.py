from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request as StarletteRequest

from .domain_approval import DomainApprovalDecision, DomainApprovalPolicy, approval_status_response, hash_bytes, now_ts
from .domain_approval_store import DomainApprovalStore


SIGNATURE_WINDOW_SEC = 300


def sign_request(method: str, path: str, body: bytes, *, timestamp: int, nonce: str, key: bytes) -> str:
    body_hash = hash_bytes(body)
    msg = f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body_hash}".encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()


def verify_hmac_headers(
    *,
    method: str,
    path: str,
    body: bytes,
    headers: dict[str, str],
    policy: DomainApprovalPolicy | None = None,
    store: DomainApprovalStore | None = None,
) -> dict[str, Any]:
    policy = policy or DomainApprovalPolicy.from_env()
    if not policy.hmac_key_file:
        return {"ok": False, "status": "hmac_key_missing"}
    timestamp = str(headers.get("X-Ralfloop-Timestamp") or headers.get("x-ralfloop-timestamp") or "")
    nonce = str(headers.get("X-Ralfloop-Nonce") or headers.get("x-ralfloop-nonce") or "")
    signature = str(headers.get("X-Ralfloop-Signature") or headers.get("x-ralfloop-signature") or "")
    try:
        ts = int(timestamp)
    except ValueError:
        return {"ok": False, "status": "signature_invalid"}
    if abs(now_ts() - ts) > SIGNATURE_WINDOW_SEC:
        return {"ok": False, "status": "signature_expired"}
    if not nonce or not signature:
        return {"ok": False, "status": "signature_invalid"}
    key = Path(policy.hmac_key_file).read_bytes().strip()
    expected = sign_request(method, path, body, timestamp=ts, nonce=nonce, key=key)
    if not hmac.compare_digest(expected, signature):
        return {"ok": False, "status": "signature_invalid"}
    store = store or DomainApprovalStore(policy=policy)
    if not store.register_nonce(nonce):
        store.audit("replay_detected", result="replay_detected")
        return {"ok": False, "status": "replay_detected"}
    return {"ok": True, "status": "ok"}


def handle_decision_request(
    *,
    request_id: str,
    body: bytes,
    headers: dict[str, str],
    method: str = "POST",
    path: str | None = None,
    policy: DomainApprovalPolicy | None = None,
    store: DomainApprovalStore | None = None,
    email_otp_gate: Any | None = None,
) -> dict[str, Any]:
    policy = policy or DomainApprovalPolicy.from_env()
    store = store or DomainApprovalStore(policy=policy)
    path = path or f"/domain-approvals/{request_id}/decision"
    auth = verify_hmac_headers(method=method, path=path, body=body, headers=headers, policy=policy, store=store)
    if not auth["ok"]:
        store.audit(auth["status"], request_id=request_id, result=auth["status"])
        return auth
    payload = json.loads(body.decode("utf-8"))
    decision = DomainApprovalDecision(
        request_id=request_id,
        decision=str(payload.get("decision") or ""),
        telegram_user_id=int(payload.get("telegram_user_id") or 0),
        telegram_chat_id=int(payload.get("telegram_chat_id") or 0),
        telegram_message_id=int(payload.get("telegram_message_id") or 0),
        telegram_username_optional=str(payload.get("telegram_username_optional") or ""),
        decision_reason=str(payload.get("decision_reason") or ""),
        timestamp=int(payload.get("timestamp") or time.time()),
        idempotency_key=str(payload.get("idempotency_key") or ""),
        chat_type=str(payload.get("chat_type") or "private"),
    )
    result = store.decide(decision, scope_digest_short=str(payload.get("scope_digest_short") or ""))
    if (
        result.get("status") == "approved"
        and os.getenv("RALFLOOP_EMAIL_OTP_REQUIRED", "0") == "1"
    ):
        row = store.get_request(request_id)
        if row and row.get("action") in {"send_email", "reply_email"}:
            try:
                if email_otp_gate is None:
                    from ralfloop_agent.unified_assistant.email_otp import EmailOtpGate
                    email_otp_gate = EmailOtpGate.from_environment()
                otp = email_otp_gate.request_for_approval(row)
            except Exception:
                otp = {"status": "email_otp_request_failed", "requested": False}
            result = {**result, "email_otp": otp, "email_otp_required": True}
    return result


def decision_headers(method: str, path: str, payload: dict[str, Any], *, key_file: str | Path, nonce: str, timestamp: int | None = None) -> dict[str, str]:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ts = timestamp or now_ts()
    key = Path(key_file).read_bytes().strip()
    return {
        "X-Ralfloop-Timestamp": str(ts),
        "X-Ralfloop-Nonce": nonce,
        "X-Ralfloop-Signature": sign_request(method, path, body, timestamp=ts, nonce=nonce, key=key),
    }


def register_domain_approval_routes(app: Any, *, policy: DomainApprovalPolicy | None = None) -> None:
    """Register localhost-only approval routes on an existing FastAPI app."""
    from .domain_approval_executor import cancel_approval, request_domain_approval

    policy = policy or DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)

    @app.post("/domain-approvals/requests")
    async def create_domain_approval_request(payload: dict[str, Any]) -> dict[str, Any]:
        return request_domain_approval(
            action=str(payload.get("action") or ""),
            bando_id=str(payload.get("bando_id") or ""),
            version=str(payload.get("version") or ""),
            canary_plan=payload.get("canary_plan") or None,
            send_telegram=bool(payload.get("send_telegram")),
            policy=policy,
            store=store,
        )

    @app.get("/domain-approvals/health")
    async def domain_approval_health() -> dict[str, Any]:
        return {
            "status": "ok",
            "enabled": policy.enabled,
            "auto_execute": policy.auto_execute,
            "db_configured": bool(policy.db_path),
            "audit_log_configured": bool(policy.audit_log),
            "hmac_key_configured": bool(policy.hmac_key_file),
            "allowed_user_count": len(policy.allowed_user_ids),
            "allowed_chat_count": len(policy.allowed_chat_ids),
            "require_private_chat": policy.require_private_chat,
        }

    @app.get("/domain-approvals/requests")
    async def list_domain_approval_requests() -> dict[str, Any]:
        return {"status": "ok", "requests": store.list_pending()}

    @app.get("/domain-approvals/{request_id}")
    async def get_domain_approval_request(request_id: str) -> dict[str, Any]:
        row = store.get_request(request_id)
        return {"status": "not_found", "request_id": request_id} if row is None else approval_status_response(row)

    @app.post("/domain-approvals/{request_id}/decision")
    async def decide_domain_approval_request(request_id: str, request: StarletteRequest) -> dict[str, Any]:
        body = await request.body()
        return handle_decision_request(
            request_id=request_id,
            body=body,
            headers={key: value for key, value in request.headers.items()},
            path=f"/domain-approvals/{request_id}/decision",
            policy=policy,
            store=store,
        )

    @app.post("/domain-approvals/{request_id}/cancel")
    async def cancel_domain_approval_request(request_id: str) -> dict[str, Any]:
        return cancel_approval(request_id, store=store)
