from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .domain_approval import (
    ALLOWED_ACTIONS,
    DomainApprovalAuditEvent,
    DomainApprovalDecision,
    DomainApprovalPolicy,
    DomainApprovalRequest,
    effective_approval_status,
    expired_execution_response,
    hash_value,
    new_nonce,
    new_request_id,
    now_ts,
    render_telegram_request,
    scope_digest,
    short_digest,
)
from .storage import append_jsonl


class DomainApprovalStore:
    def __init__(self, db_path: str | Path | None = None, policy: DomainApprovalPolicy | None = None) -> None:
        self.policy = policy or DomainApprovalPolicy.from_env()
        self.db_path = Path(db_path or self.policy.db_path or Path(tempfile.gettempdir()) / f"ralfloop_domain_approvals_{os.getuid()}.sqlite")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            self.ensure_schema(conn)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        return conn

    def ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            create table if not exists approval_requests (
                request_id text primary key,
                action text not null,
                bando_id text not null,
                domain_version text not null,
                created_at integer not null,
                expires_at integer not null,
                status text not null,
                requested_by text,
                domain_state text,
                git_commit text,
                domain_manifest_hash text,
                domain_content_hash text,
                source_set_hash text,
                rule_set_hash text,
                test_evidence_hash text,
                canary_plan_hash text,
                promotion_readiness_hash text,
                scope_digest text not null,
                scope_digest_short text not null,
                nonce_hash text not null,
                consumed_at integer,
                scope_json text not null,
                telegram_message_json text
            );
            create table if not exists approval_decisions (
                decision_id integer primary key autoincrement,
                request_id text not null,
                decision text not null,
                telegram_user_id_hash text not null,
                telegram_chat_id_hash text not null,
                telegram_message_id text,
                telegram_username_optional text,
                decision_reason text,
                idempotency_key_hash text not null,
                created_at integer not null,
                result_json text not null,
                unique(idempotency_key_hash)
            );
            create table if not exists approval_executions (
                execution_id integer primary key autoincrement,
                request_id text not null,
                action text not null,
                dry_run integer not null,
                status text not null,
                created_at integer not null,
                result_json text not null
            );
            create table if not exists approval_audit_events (
                event_id integer primary key autoincrement,
                created_at integer not null,
                event_json text not null
            );
            create table if not exists approval_nonces (
                nonce_hash text primary key,
                created_at integer not null
            );
            """
        )

    def create_request(self, *, action: str, bando_id: str, version: str, scope: dict[str, Any], requested_by: str = "cli") -> dict[str, Any]:
        if action not in ALLOWED_ACTIONS:
            return {"status": "input_invalid", "error": "unsupported_action"}
        pending = self.count_pending()
        if pending >= self.policy.max_pending:
            return {"status": "too_many_pending", "max_pending": self.policy.max_pending}
        now = now_ts()
        full_digest = scope_digest(scope)
        nonce = new_nonce()
        request = DomainApprovalRequest(
            request_id=new_request_id(),
            action=action,
            bando_id=bando_id,
            domain_version=version,
            created_at=now,
            expires_at=now + self.policy.ttl_sec,
            status="pending",
            requested_by=requested_by,
            domain_state=str(scope.get("domain_state") or ""),
            git_commit=str(scope.get("git_commit") or ""),
            domain_manifest_hash=str(scope.get("domain_manifest_hash") or ""),
            domain_content_hash=str(scope.get("domain_content_hash") or ""),
            source_set_hash=str(scope.get("source_set_hash") or ""),
            rule_set_hash=str(scope.get("rule_set_hash") or ""),
            test_evidence_hash=str(scope.get("test_evidence_hash") or ""),
            canary_plan_hash=str(scope.get("canary_plan_hash") or ""),
            promotion_readiness_hash=str(scope.get("promotion_readiness_hash") or ""),
            scope_digest=full_digest,
            scope_digest_short=short_digest(full_digest),
            nonce_hash=hash_value(nonce),
            scope=scope,
        )
        message = render_telegram_request(request)
        with self.connect() as conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                insert into approval_requests values (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    request.request_id,
                    request.action,
                    request.bando_id,
                    request.domain_version,
                    request.created_at,
                    request.expires_at,
                    request.status,
                    request.requested_by,
                    request.domain_state,
                    request.git_commit,
                    request.domain_manifest_hash,
                    request.domain_content_hash,
                    request.source_set_hash,
                    request.rule_set_hash,
                    request.test_evidence_hash,
                    request.canary_plan_hash,
                    request.promotion_readiness_hash,
                    request.scope_digest,
                    request.scope_digest_short,
                    request.nonce_hash,
                    request.consumed_at,
                    _json(request.scope),
                    _json({"message": message}),
                ),
            )
            conn.commit()
        self.audit("approval_requested", request_id=request.request_id, action=action, bando_id=bando_id, version=version, new_status="pending", scope_digest=full_digest)
        data = request.to_dict()
        data["telegram_message"] = message
        data["nonce"] = nonce
        return {"status": "pending", "request": data}

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("select * from approval_requests where request_id = ?", (request_id,)).fetchone()
        return _row_request(row) if row else None

    def list_pending(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute("select * from approval_requests where status = 'pending' order by created_at").fetchall()
        return [_row_request(row) for row in rows]

    def count_pending(self) -> int:
        with self.connect() as conn:
            row = conn.execute("select count(*) as c from approval_requests where status = 'pending'").fetchone()
        return int(row["c"])

    def decide(self, decision: DomainApprovalDecision, *, scope_digest_short: str) -> dict[str, Any]:
        if decision.decision not in {"approve", "reject"}:
            return {"status": "input_invalid", "error": "unsupported_decision"}
        idem = hash_value(decision.idempotency_key or f"{decision.telegram_chat_id}:{decision.telegram_message_id}:{decision.request_id}")
        with self.connect() as conn:
            conn.execute("begin immediate")
            prev = conn.execute("select result_json from approval_decisions where idempotency_key_hash = ?", (idem,)).fetchone()
            if prev:
                conn.commit()
                return json.loads(prev["result_json"])
            row = conn.execute("select * from approval_requests where request_id = ?", (decision.request_id,)).fetchone()
            if not row:
                result = {"status": "not_found", "request_id": decision.request_id}
                self._insert_decision(conn, decision, idem, result)
                conn.commit()
                return result
            req = _row_request(row)
            auth = self._authorize_decision(decision)
            if not auth["ok"]:
                result = {"status": auth["status"], "request_id": decision.request_id}
                self._insert_decision(conn, decision, idem, result)
                conn.commit()
                self.audit("unauthorized_decision_attempt", request_id=decision.request_id, action=req["action"], bando_id=req["bando_id"], version=req["domain_version"], telegram_user_id=decision.telegram_user_id, result=result["status"])
                return result
            if req["scope_digest_short"] != scope_digest_short:
                result = {"status": "scope_digest_mismatch", "request_id": decision.request_id}
            elif effective_approval_status(req) == "expired":
                if req["status"] == "pending":
                    self._set_status(conn, decision.request_id, "expired")
                result = {"status": "expired", "request_id": decision.request_id}
            elif req["status"] == "approved" and decision.decision == "approve":
                result = {"status": "already_approved", "request_id": decision.request_id}
            elif req["status"] == "rejected" and decision.decision == "reject":
                result = {"status": "already_rejected", "request_id": decision.request_id}
            elif req["status"] in {"approved", "rejected", "consumed", "executed", "cancelled", "stale", "expired"}:
                result = {"status": f"already_{req['status']}", "request_id": decision.request_id}
            elif decision.decision == "approve":
                self._set_status(conn, decision.request_id, "approved")
                result = {"status": "approved", "request_id": decision.request_id, "auto_execute": False}
            else:
                self._set_status(conn, decision.request_id, "rejected")
                result = {"status": "rejected", "request_id": decision.request_id}
            self._insert_decision(conn, decision, idem, result)
            conn.commit()
        event = "approval_approved" if result["status"] == "approved" else "approval_rejected" if result["status"] == "rejected" else result["status"]
        self.audit(event, request_id=decision.request_id, action=req["action"], bando_id=req["bando_id"], version=req["domain_version"], telegram_user_id=decision.telegram_user_id, old_status=req["status"], new_status=result["status"], idempotency_key=decision.idempotency_key, result=result["status"], reason=decision.decision_reason)
        return result

    def cancel(self, request_id: str) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select * from approval_requests where request_id = ?", (request_id,)).fetchone()
            if not row:
                conn.commit()
                return {"status": "not_found", "request_id": request_id}
            req = _row_request(row)
            if req["status"] != "pending":
                conn.commit()
                return {"status": f"already_{req['status']}", "request_id": request_id}
            self._set_status(conn, request_id, "cancelled")
            conn.commit()
        self.audit("approval_cancelled", request_id=request_id, old_status=req["status"], new_status="cancelled")
        return {"status": "cancelled", "request_id": request_id}

    def mark_stale(self, request_id: str, reasons: list[str]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("begin immediate")
            self._set_status(conn, request_id, "stale")
            conn.commit()
        self.audit("approval_stale", request_id=request_id, new_status="stale", reason=",".join(reasons))
        return {"status": "stale", "request_id": request_id, "stale_reasons": reasons, "execution_allowed": False}

    def consume(self, request_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with self.connect() as conn:
            conn.execute("begin immediate")
            row = conn.execute("select * from approval_requests where request_id = ?", (request_id,)).fetchone()
            if not row:
                conn.commit()
                return {"status": "not_found"}
            req = _row_request(row)
            effective = effective_approval_status(req)
            if effective == "expired":
                conn.commit()
                result = expired_execution_response(req)
                self.audit("execution_blocked_expired", request_id=request_id, action=req["action"], old_status=req["status"], new_status="expired", result="expired")
                return result
            if effective != "approved":
                conn.commit()
                return {"status": f"not_approved:{effective}"}
            conn.execute("update approval_requests set status = 'consumed', consumed_at = ? where request_id = ?", (now_ts(), request_id))
            conn.execute("insert into approval_executions (request_id, action, dry_run, status, created_at, result_json) values (?, ?, ?, ?, ?, ?)", (request_id, result.get("action", ""), 0, result.get("status", "executed"), now_ts(), _json(result)))
            conn.commit()
        self.audit("approval_consumed", request_id=request_id, new_status="consumed", result=result.get("status", ""))
        return {"status": "consumed", "request_id": request_id}

    def record_execution(self, request_id: str, action: str, dry_run: bool, status: str, result: dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute("insert into approval_executions (request_id, action, dry_run, status, created_at, result_json) values (?, ?, ?, ?, ?, ?)", (request_id, action, int(dry_run), status, now_ts(), _json(result)))
        self.audit("execution_completed" if status in {"dry_run", "executed"} else "execution_failed", request_id=request_id, action=action, result=status)

    def register_nonce(self, nonce: str) -> bool:
        digest = hash_value(nonce)
        with self.connect() as conn:
            conn.execute("begin immediate")
            exists = conn.execute("select nonce_hash from approval_nonces where nonce_hash = ?", (digest,)).fetchone()
            if exists:
                conn.commit()
                return False
            conn.execute("insert into approval_nonces values (?, ?)", (digest, now_ts()))
            conn.commit()
        return True

    def audit(self, event: str, **fields: Any) -> None:
        record = DomainApprovalAuditEvent(timestamp=now_ts(), event=event, request_id=str(fields.get("request_id") or ""), action=str(fields.get("action") or ""), bando_id=str(fields.get("bando_id") or ""), version=str(fields.get("version") or ""), telegram_user_id_hash=hash_value(str(fields.get("telegram_user_id"))) if fields.get("telegram_user_id") else "", chat_type=str(fields.get("chat_type") or ""), scope_digest=str(fields.get("scope_digest") or ""), old_status=str(fields.get("old_status") or ""), new_status=str(fields.get("new_status") or ""), idempotency_key_hash=hash_value(str(fields.get("idempotency_key"))) if fields.get("idempotency_key") else "", result=str(fields.get("result") or ""), reason=str(fields.get("reason") or "")).to_dict()
        with self.connect() as conn:
            conn.execute("insert into approval_audit_events (created_at, event_json) values (?, ?)", (record["timestamp"], _json(record)))
        try:
            append_jsonl(Path(self.policy.audit_log), record)
        except Exception:
            pass

    def _authorize_decision(self, decision: DomainApprovalDecision) -> dict[str, Any]:
        if self.policy.allowed_user_ids and decision.telegram_user_id not in self.policy.allowed_user_ids:
            return {"ok": False, "status": "unauthorized"}
        if self.policy.allowed_chat_ids and decision.telegram_chat_id not in self.policy.allowed_chat_ids:
            return {"ok": False, "status": "chat_unauthorized"}
        if self.policy.require_private_chat and decision.chat_type != "private":
            return {"ok": False, "status": "private_chat_required"}
        return {"ok": True}

    def _insert_decision(self, conn: sqlite3.Connection, decision: DomainApprovalDecision, idem: str, result: dict[str, Any]) -> None:
        conn.execute(
            "insert into approval_decisions (request_id, decision, telegram_user_id_hash, telegram_chat_id_hash, telegram_message_id, telegram_username_optional, decision_reason, idempotency_key_hash, created_at, result_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                decision.request_id,
                decision.decision,
                hash_value(str(decision.telegram_user_id)),
                hash_value(str(decision.telegram_chat_id)),
                str(decision.telegram_message_id),
                decision.telegram_username_optional,
                decision.decision_reason,
                idem,
                decision.timestamp or now_ts(),
                _json(result),
            ),
        )

    def _set_status(self, conn: sqlite3.Connection, request_id: str, status: str) -> None:
        conn.execute("update approval_requests set status = ? where request_id = ?", (status, request_id))


def _row_request(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["scope"] = json.loads(data.pop("scope_json") or "{}")
    data["telegram_message"] = json.loads(data.pop("telegram_message_json") or "{}")
    return data


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
