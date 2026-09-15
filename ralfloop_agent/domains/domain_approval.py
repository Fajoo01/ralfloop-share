from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


ALLOWED_ACTIONS = {
    "eyf_browser_apply",
    "promote_domain", "run_domain_canary", "apply_domain_source_update",
    "dispatch_glm_artifact", "reply_email", "send_email",
    "whatsapp_send", "whatsapp_reply",
    "local_maintenance_apply", "repair_apply",
    "mailchimp_campaign_create", "mailchimp_campaign_send", "mailchimp_member_subscribe",
    "runts_practice_reply",
}
FINAL_STATUSES = {"rejected", "expired", "stale", "consumed", "cancelled", "execution_failed", "executed"}


@dataclass
class DomainApprovalPolicy:
    enabled: bool = False
    auto_execute: bool = False
    ttl_sec: int = 3600
    max_pending: int = 20
    allowed_user_ids: set[int] = field(default_factory=set)
    allowed_chat_ids: set[int] = field(default_factory=set)
    require_private_chat: bool = True
    db_path: str = ""
    audit_log: str = "logs/domain_approval_audit.jsonl"
    hmac_key_file: str = ""
    api_url: str = "http://127.0.0.1:19090"

    @classmethod
    def from_env(cls) -> "DomainApprovalPolicy":
        return cls(
            enabled=os.getenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "0") == "1",
            auto_execute=os.getenv("RALFLOOP_TELEGRAM_APPROVAL_AUTO_EXECUTE", "0") == "1",
            ttl_sec=_env_int("RALFLOOP_TELEGRAM_APPROVAL_TTL_SEC", 3600),
            max_pending=_env_int("RALFLOOP_TELEGRAM_APPROVAL_MAX_PENDING", 20),
            allowed_user_ids=_parse_ids(os.getenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "")),
            allowed_chat_ids=_parse_ids(os.getenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_CHAT_IDS", "")),
            require_private_chat=os.getenv("RALFLOOP_TELEGRAM_APPROVAL_REQUIRE_PRIVATE_CHAT", "1") == "1",
            db_path=os.getenv("RALFLOOP_TELEGRAM_APPROVAL_DB", ""),
            audit_log=os.getenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", "logs/domain_approval_audit.jsonl"),
            hmac_key_file=os.getenv("RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE", ""),
            api_url=os.getenv("RALFLOOP_TELEGRAM_APPROVAL_API_URL", "http://127.0.0.1:19090"),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["allowed_user_ids"] = sorted(self.allowed_user_ids)
        data["allowed_chat_ids"] = sorted(self.allowed_chat_ids)
        data["hmac_key_file"] = "<configured>" if self.hmac_key_file else ""
        return data


@dataclass
class DomainApprovalScope:
    action: str
    bando_id: str
    version: str
    git_commit: str
    domain_state: str = ""
    domain_path: str = ""
    source_draft_path: str = ""
    destination_active_path: str = ""
    registry_path: str = ""
    domain_manifest_hash: str = ""
    domain_content_hash: str = ""
    source_set_hash: str = ""
    rule_set_hash: str = ""
    test_evidence_hash: str = ""
    canary_plan_hash: str = ""
    promotion_readiness_hash: str = ""
    canary_plan: dict[str, Any] = field(default_factory=dict)
    validation_summary: dict[str, Any] = field(default_factory=dict)
    known_uncovered_cases: list[dict[str, Any]] = field(default_factory=list)
    known_conflicts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainApprovalRequest:
    request_id: str
    action: str
    bando_id: str
    domain_version: str
    created_at: int
    expires_at: int
    status: str
    requested_by: str
    domain_state: str
    git_commit: str
    domain_manifest_hash: str
    domain_content_hash: str
    source_set_hash: str
    rule_set_hash: str
    test_evidence_hash: str
    canary_plan_hash: str
    promotion_readiness_hash: str
    scope_digest: str
    scope_digest_short: str
    nonce_hash: str
    consumed_at: int | None = None
    scope: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainApprovalDecision:
    request_id: str
    decision: str
    telegram_user_id: int
    telegram_chat_id: int
    telegram_message_id: int
    chat_type: str = "private"
    telegram_username_optional: str = ""
    decision_reason: str = ""
    idempotency_key: str = ""
    timestamp: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainApprovalExecution:
    request_id: str
    action: str
    dry_run: bool
    status: str
    execution_allowed: bool
    result: dict[str, Any] = field(default_factory=dict)
    stale_reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainApprovalStatus:
    request_id: str
    status: str
    action: str
    bando_id: str
    version: str
    scope_digest_short: str
    expires_at: int
    consumed_at: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainApprovalAuditEvent:
    timestamp: int
    event: str
    request_id: str
    action: str = ""
    bando_id: str = ""
    version: str = ""
    actor_type: str = "system"
    telegram_user_id_hash: str = ""
    chat_type: str = ""
    scope_digest: str = ""
    old_status: str = ""
    new_status: str = ""
    idempotency_key_hash: str = ""
    result: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def new_request_id() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "apr_" + "".join(secrets.choice(alphabet) for _ in range(8))


def new_nonce() -> str:
    return secrets.token_urlsafe(24)


def scope_digest(scope: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(scope).encode("utf-8")).hexdigest()


def short_digest(full_digest: str) -> str:
    up = full_digest.upper()
    return f"{up[:4]}-{up[4:8]}"


def hash_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_path(path: Path) -> str:
    if not path.exists():
        return ""
    if path.is_file():
        return hash_bytes(path.read_bytes())
    h = hashlib.sha256()
    for file in sorted(item for item in path.rglob("*") if item.is_file()):
        h.update(str(file.relative_to(path)).encode("utf-8"))
        h.update(b"\0")
        h.update(file.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def effective_approval_status(request: dict[str, Any], *, now: int | None = None) -> str:
    current = now_ts() if now is None else int(now)
    if request.get("consumed_at") is not None:
        return "consumed"
    try:
        expires_at = int(request.get("expires_at") or 0)
    except (TypeError, ValueError):
        expires_at = 0
    if current >= expires_at:
        return "expired"
    return str(request.get("status") or "")


def approval_status_response(request: dict[str, Any], *, now: int | None = None) -> dict[str, Any]:
    effective = effective_approval_status(request, now=now)
    stored = str(request.get("status") or "")
    return {
        "status": effective,
        "stored_status": stored,
        "effective_status": effective,
        "expired": effective == "expired",
        "request": request,
    }


def expired_execution_response(request: dict[str, Any]) -> dict[str, Any]:
    effective = effective_approval_status(request)
    return {
        "request_id": request.get("request_id"),
        "status": "expired",
        "stored_status": request.get("status"),
        "effective_status": effective,
        "expired": True,
        "consumed": bool(request.get("consumed_at")),
        "would_execute": False,
        "execution_allowed": False,
    }


def render_telegram_request(request: DomainApprovalRequest) -> str:
    action_label = {
        "run_domain_canary": "Canary dominio",
        "promote_domain": "Promozione dominio",
        "apply_domain_source_update": "Aggiornamento fonti/regole",
        "dispatch_glm_artifact": "Azione esterna su artifact GLM",
        "local_maintenance_apply": "Manutenzione locale protetta",
        "repair_apply": "Applicazione patch locale verificata",
        "eyf_browser_apply": "Modifica portale EYF/Support4Youth",
    }.get(request.action, request.action)
    lines = [
        "RALFLOOP — APPROVAZIONE RICHIESTA",
        "",
        f"Azione: {action_label}",
        f"Bando: {request.bando_id}",
        f"Versione: {request.domain_version}",
        f"Request: {request.request_id}",
        f"Digest: {request.scope_digest_short}",
        f"Scadenza: {time.strftime('%d/%m/%Y %H:%M UTC', time.gmtime(request.expires_at))}",
    ]
    canary = request.scope.get("canary_plan") or {}
    if canary:
        lines.extend(
            [
                "",
                "Canary:",
                str(canary.get("exact_input") or canary.get("canary_query") or "")[:500],
                "",
                "Regola attesa:",
                str(canary.get("expected_rule_id") or canary.get("expected_rule_ids") or ""),
                "",
                "Risposta attesa:",
                str(canary.get("expected_deterministic_answer") or canary.get("expected_answer") or ""),
                "",
                f"Giuria: {'sì' if canary.get('jury_expected') else 'no'}",
                f"RecursiveMAS: {'sì' if canary.get('recursive_mas_expected') else 'no'}",
                f"Legacy fallback: {'sì' if canary.get('legacy_fallback_expected') else 'no'}",
            ]
        )
    if request.action == "eyf_browser_apply":
        phase = str(request.scope.get("approval_phase") or "unknown")
        lines.extend(
            [
                "",
                f"Fase portale: {phase}",
                "Operazioni esatte:",
            ]
        )
        summary = request.scope.get("approval_summary") or []
        if isinstance(summary, list):
            lines.extend(f"- {str(item)}" for item in summary)
        if phase == "final_submission":
            lines.extend(
                [
                    "",
                    "FINAL SUBMISSION: Send updates",
                    "Autorizza il controllo di entrambe le dichiarazioni:",
                    "- accettazione termini",
                    "- accettazione trattamento dati",
                ]
            )
    if request.action == "runts_practice_reply":
        practice_id = str(request.scope.get("practice_id") or "").strip()
        lines.extend(
            [
                "",
                "Per approvare dal flusso RUNTS:",
                f"Approvo {practice_id}" if practice_id else "Approvo <practice_id>",
                "",
                "L'approvazione RUNTS richiede pratica e scope hash-bound.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Per approvare:",
                f"rl:approve {request.request_id} {request.scope_digest_short}",
                "Oppure rispondi a questo messaggio con: approvo",
                "",
                "Per rifiutare:",
                f"rl:reject {request.request_id} {request.scope_digest_short} MOTIVO",
                "Oppure rispondi a questo messaggio con: rifiuto MOTIVO",
                "",
                "L'approvazione autorizza solo questo scope e non esegue automaticamente l'azione.",
            ]
        )
    return "\n".join(lines)


def now_ts() -> int:
    return int(time.time())


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _parse_ids(raw: str) -> set[int]:
    out = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default
