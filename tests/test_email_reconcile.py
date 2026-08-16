from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
from email.utils import format_datetime
import hashlib
import json
import sqlite3
import time

import pytest

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.email_reconcile import EmailReconciler
from ralfloop_agent.cli.terminal_chat import build_parser


ACCOUNT = "fabio@tiremminnanz.com"
RECIPIENT = "caterina@circolomagnolia.it"
SUBJECT = "Invito partecipazione Magnolia"
BODY = "Grazie. Siamo interessati; il contributo concreto resta da definire."
THREAD = "19fd1fbc9ff936d0"
MESSAGE = "19fd1fbc9ff936d9"


class Gateway:
    account = ACCOUNT

    def __init__(self, mode: str, authorized_at: int) -> None:
        self.mode = mode
        self.calls: list[tuple[str, dict]] = []
        self.date = format_datetime(datetime.fromtimestamp(authorized_at + 10, timezone.utc))

    def invoke(self, operation: str, **arguments):
        self.calls.append((operation, dict(arguments)))
        assert operation in {"getThread", "search", "read"}
        if self.mode == "error":
            raise OSError("mcp_down")
        incoming = {
            "from": f"Caterina <{RECIPIENT}>", "to": ACCOUNT,
            "subject": SUBJECT, "date": format_datetime(datetime.now(timezone.utc)),
            "body": "Invito.",
        }
        outgoing = {
            "messageId": MESSAGE, "threadId": THREAD,
            "from": f"Fabio <{ACCOUNT}>", "to": RECIPIENT,
            "subject": SUBJECT, "date": self.date, "body": BODY,
        }
        if operation == "getThread":
            messages = [incoming]
            if self.mode in {"sent", "ambiguous"}:
                messages.append(outgoing)
            return {"threadId": THREAD, "messages": messages}
        if operation == "search":
            if self.mode in {"sent", "ambiguous"}:
                return {"messages": [{
                    "messageId": MESSAGE, "subject": SUBJECT, "date": self.date,
                }]}
            return {"messages": []}
        if self.mode == "ambiguous":
            return {"message": {**outgoing, "body": "Different body"}}
        return {"message": outgoing}


class Context(AbstractContextManager):
    def __init__(self, gateway: Gateway) -> None:
        self.gateway = gateway

    def __enter__(self):
        return self.gateway

    def __exit__(self, *_):
        return None


def _fixture(tmp_path, *, mode="sent", status="stale", expired=False):
    now = int(time.time())
    policy = DomainApprovalPolicy(
        enabled=True, ttl_sec=300, max_pending=10,
        allowed_user_ids={11}, allowed_chat_ids={22},
        db_path=str(tmp_path / "approvals.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    store = DomainApprovalStore(policy=policy)
    scope = {
        "action": "reply_email", "version": 1, "account": ACCOUNT,
        "recipient": RECIPIENT, "subject": SUBJECT, "body": BODY,
        "body_sha256": hashlib.sha256(BODY.encode()).hexdigest(),
        "thread_id": THREAD, "source_message_id": THREAD,
    }
    created = store.create_request(
        action="reply_email", bando_id="google_workspace.gmail",
        version="1", scope=scope, requested_by="test",
    )["request"]
    approved = store.decide(DomainApprovalDecision(
        request_id=created["request_id"], decision="approve",
        telegram_user_id=11, telegram_chat_id=22, telegram_message_id=33,
        idempotency_key="email-reconcile-test",
    ), scope_digest_short=created["scope_digest_short"])
    assert approved["status"] == "approved"
    with store.connect() as conn:
        conn.execute(
            "update approval_requests set status = ?, expires_at = ? where request_id = ?",
            (status, now - 1 if expired else now + 300, created["request_id"]),
        )
    otp_db = tmp_path / "otp.sqlite"
    binding_db = tmp_path / "binding.sqlite"
    otp_audit = tmp_path / "otp-audit.jsonl"
    otp_scope = {"to": [RECIPIENT], "cc": [], "bcc": [], "subject": SUBJECT, "attachments": []}
    otp_json = json.dumps(otp_scope, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(otp_db) as conn:
        conn.execute("create table email_otp_requests (request_id text primary key, created_at integer, expires_at integer, status text, attempts integer, max_attempts integer, otp_salt text, otp_digest text, scope_json text, scope_digest text, approved_at integer, consumed_at integer)")
        conn.execute("insert into email_otp_requests values (?,?,?,?,?,?,?,?,?,?,?,?)", (
            "mailotp_test", now - 20, now + 280, "consumed", 1, 3,
            "redacted", "redacted", otp_json, "digest", now - 10, now - 10,
        ))
    with sqlite3.connect(binding_db) as conn:
        conn.execute("create table email_otp_bindings (approval_request_id text primary key, otp_request_id text, otp_scope_json text, status text)")
        conn.execute("insert into email_otp_bindings values (?,?,?,?)", (
            created["request_id"], "mailotp_test", otp_json, "consumed",
        ))
    otp_audit.write_text("".join(
        json.dumps({
            "timestamp": now - offset, "event": event,
            "request_id": "mailotp_test", "status": status,
            "scope_digest": "digest",
        }) + "\n"
        for offset, event, status in (
            (20, "otp_requested", "pending"),
            (10, "otp_approved", "approved"),
            (10, "otp_consumed", "consumed"),
        )
    ), encoding="utf-8")
    gateway = Gateway(mode, now - 10)
    reconciler = EmailReconciler(
        store=store, gateway_factory=lambda _account: Context(gateway),
        otp_db_path=otp_db, otp_binding_db_path=binding_db,
        otp_audit_path=otp_audit,
    )
    return reconciler, store, gateway, created["request_id"], otp_db


def _run(reconciler, approval_id):
    return reconciler.reconcile(
        approval_id=approval_id, otp_request_id="mailotp_test", thread_id=THREAD
    )


def _assert_read_only(gateway):
    assert gateway.calls
    assert {operation for operation, _ in gateway.calls} <= {"getThread", "search", "read"}


def test_sent_confirmed_reconciles_without_send_and_is_idempotent(tmp_path):
    reconciler, store, gateway, approval_id, _ = _fixture(tmp_path)

    first = _run(reconciler, approval_id)
    second = _run(reconciler, approval_id)

    assert first.final == second.final == "SENT_CONFIRMED"
    assert first.provider_message_id == MESSAGE
    assert first.send_calls_during_reconcile == 0
    assert second.execution_count == first.execution_count == 1
    assert store.get_request(approval_id)["status"] == "consumed"
    _assert_read_only(gateway)


def test_not_sent_confirmed_never_sends_or_changes_approval(tmp_path):
    reconciler, store, gateway, approval_id, _ = _fixture(tmp_path, mode="not_sent")

    result = _run(reconciler, approval_id)

    assert result.final == "NOT_SENT_CONFIRMED"
    assert result.execution_count == 0
    assert store.get_request(approval_id)["status"] == "stale"
    _assert_read_only(gateway)


def test_ambiguous_candidate_never_sends(tmp_path):
    reconciler, store, gateway, approval_id, _ = _fixture(tmp_path, mode="ambiguous")

    result = _run(reconciler, approval_id)

    assert result.final == "AMBIGUOUS"
    assert result.execution_count == 0
    assert store.get_request(approval_id)["status"] == "stale"
    _assert_read_only(gateway)


def test_consumed_otp_is_observed_but_never_reused_or_mutated(tmp_path):
    reconciler, _, gateway, approval_id, otp_db = _fixture(tmp_path, mode="not_sent")
    before = otp_db.read_bytes()

    result = _run(reconciler, approval_id)

    assert result.otp_status == "consumed"
    assert otp_db.read_bytes() == before
    _assert_read_only(gateway)


def test_expired_approval_is_not_reactivated(tmp_path):
    reconciler, store, gateway, approval_id, _ = _fixture(
        tmp_path, mode="not_sent", status="approved", expired=True
    )

    result = _run(reconciler, approval_id)

    assert result.final == "NOT_SENT_CONFIRMED"
    assert store.get_request(approval_id)["status"] == "approved"
    assert result.execution_count == 0
    _assert_read_only(gateway)


def test_mcp_error_is_ambiguous_and_zero_send(tmp_path):
    reconciler, store, gateway, approval_id, _ = _fixture(tmp_path, mode="error")

    result = _run(reconciler, approval_id)

    assert result.final == "AMBIGUOUS"
    assert store.get_request(approval_id)["status"] == "stale"
    _assert_read_only(gateway)


def test_spurious_confirmation_id_cannot_authorize_gmail(tmp_path):
    reconciler, _, gateway, _, _ = _fixture(tmp_path)

    result = _run(reconciler, "f41c1ff1-4715-44e0-a702-0b42c2a016a1")

    assert result.final == "AMBIGUOUS"
    assert result.approval_status == "missing"
    assert gateway.calls == []


def test_cli_exposes_only_explicit_approval_otp_and_thread_binding():
    args = build_parser().parse_args([
        "email", "reconcile", "--approval", "apr_test",
        "--otp-request", "mailotp_test", "--thread", THREAD,
    ])

    assert args.command == "email"
    assert args.email_action == "reconcile"
    assert args.approval == "apr_test"
    assert args.otp_request == "mailotp_test"
    assert args.thread == THREAD


@pytest.mark.parametrize("field,value", [
    ("threadId", "aaaaaaaaaaaaaaaa"),
    ("to", "other@example.invalid"),
    ("subject", "Different subject"),
    ("body", "Different body"),
    ("from", "Other <other@example.invalid>"),
])
def test_adversarial_provider_metadata_never_confirms(tmp_path, field, value):
    reconciler, _, gateway, approval_id, _ = _fixture(tmp_path)
    original = gateway.invoke

    def altered(operation, **arguments):
        out = original(operation, **arguments)
        if operation == "read":
            out["message"][field] = value
        return out

    gateway.invoke = altered

    assert _run(reconciler, approval_id).final == "AMBIGUOUS"
    _assert_read_only(gateway)
