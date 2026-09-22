from __future__ import annotations

from pathlib import Path

from ralfloop_agent.domains.domain_approval import DomainApprovalDecision, DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.unified_assistant.pec_smtp_writer import PecSmtpConfig, PecSmtpWriter
from scripts.ralf_pec_write_mcp_server import PecWriteMCPServer, _response


class FakeSMTP:
    sent_messages = 0
    logins = 0

    def __init__(self, *args, **kwargs):
        self.closed = False

    def ehlo(self):
        return 250, b"ok"

    def login(self, username, password):
        assert username == "tiremminnanz@pec.it"
        assert password == "secret"
        type(self).logins += 1
        return 235, b"ok"

    def noop(self):
        return 250, b"ok"

    def send_message(self, message, *, from_addr, to_addrs):
        assert from_addr == "tiremminnanz@pec.it"
        assert to_addrs == ["difensore@example.test"]
        assert message["Subject"] == "Pratica TARI"
        type(self).sent_messages += 1
        return {}

    def quit(self):
        self.closed = True
        return 221, b"bye"


def _writer(tmp_path: Path):
    FakeSMTP.sent_messages = 0
    FakeSMTP.logins = 0
    policy = DomainApprovalPolicy(
        enabled=True,
        ttl_sec=3600,
        max_pending=20,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        require_private_chat=True,
        db_path=str(tmp_path / "approval.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )
    store = DomainApprovalStore(tmp_path / "approval.sqlite", policy=policy)
    config = PecSmtpConfig(
        host="smtp.example.test",
        port=465,
        username="tiremminnanz@pec.it",
        password="secret",
        timeout=1,
        attachment_roots=(tmp_path,),
    )
    return PecSmtpWriter(config, store=store, smtp_factory=FakeSMTP), store


def _approve(store: DomainApprovalStore, prepared: dict, *, message_id: int = 33):
    return store.decide(
        DomainApprovalDecision(
            request_id=prepared["approval_request_id"],
            decision="approve",
            telegram_user_id=11,
            telegram_chat_id=22,
            telegram_message_id=message_id,
            chat_type="private",
            idempotency_key=f"pec-test-{message_id}",
        ),
        scope_digest_short=prepared["scope_digest_short"],
    )


def test_preflight_authenticates_but_never_sends(tmp_path):
    writer, _ = _writer(tmp_path)
    result = writer.preflight()
    assert result["status"] == "ready"
    assert result["writes"] == 0 and result["sends"] == 0
    assert FakeSMTP.logins == 1
    assert FakeSMTP.sent_messages == 0


def test_prepare_creates_hash_bound_approval_without_sending(tmp_path):
    writer, store = _writer(tmp_path)
    attachment = tmp_path / "istanza.pdf"
    attachment.write_bytes(b"pdf")
    result = writer.prepare_send(
        recipient="difensore@example.test",
        subject="Pratica TARI",
        body="Testo verificato",
        attachment_paths=(str(attachment),),
    )
    assert result["status"] == "approval_required"
    assert result["writes"] == 0 and result["sends"] == 0
    row = store.get_request(result["approval_request_id"])
    assert row and row["status"] == "pending" and row["action"] == "pec_send"
    assert FakeSMTP.sent_messages == 0


def test_unapproved_send_is_blocked_before_smtp(tmp_path):
    writer, _ = _writer(tmp_path)
    prepared = writer.prepare_send(
        recipient="difensore@example.test",
        subject="Pratica TARI",
        body="Testo verificato",
    )
    result = writer.send_approved(
        approval_request_id=prepared["approval_request_id"],
        recipient="difensore@example.test",
        subject="Pratica TARI",
        body="Testo verificato",
    )
    assert result["status"] == "approval_pending"
    assert result["writes"] == 0 and result["sends"] == 0
    assert FakeSMTP.sent_messages == 0


def test_changed_draft_invalidates_approval_and_never_sends(tmp_path):
    writer, store = _writer(tmp_path)
    prepared = writer.prepare_send(
        recipient="difensore@example.test",
        subject="Pratica TARI",
        body="Versione approvata",
    )
    assert _approve(store, prepared)["status"] == "approved"
    result = writer.send_approved(
        approval_request_id=prepared["approval_request_id"],
        recipient="difensore@example.test",
        subject="Pratica TARI",
        body="Versione cambiata",
    )
    assert result["status"] == "approval_scope_mismatch"
    assert store.get_request(prepared["approval_request_id"])["status"] == "stale"
    assert FakeSMTP.sent_messages == 0


def test_approved_send_is_one_shot_and_receipt_stays_unverified(tmp_path):
    writer, store = _writer(tmp_path)
    prepared = writer.prepare_send(
        recipient="difensore@example.test",
        subject="Pratica TARI",
        body="Versione approvata",
    )
    assert _approve(store, prepared)["status"] == "approved"
    first = writer.send_approved(
        approval_request_id=prepared["approval_request_id"],
        recipient="difensore@example.test",
        subject="Pratica TARI",
        body="Versione approvata",
    )
    assert first["status"] == "submitted_to_pec_provider"
    assert first["sends"] == 1 and first["delivery_verified"] is False
    second = writer.send_approved(
        approval_request_id=prepared["approval_request_id"],
        recipient="difensore@example.test",
        subject="Pratica TARI",
        body="Versione approvata",
    )
    assert second["sends"] == 0
    assert FakeSMTP.sent_messages == 1


def test_writer_mcp_inventory_is_separate_and_exact(tmp_path):
    writer, _ = _writer(tmp_path)
    server = PecWriteMCPServer(writer)
    init = _response({"jsonrpc": "2.0", "id": 1, "method": "initialize"}, server)
    assert init["result"]["serverInfo"]["name"] == "ralf-pec-write"
    tools = _response({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, server)
    assert {row["name"] for row in tools["result"]["tools"]} == {
        "pec_writer_preflight", "pec_prepare_send", "pec_send_approved"
    }


def test_writer_systemd_unit_is_private_and_release_bound():
    unit = Path("deploy/systemd/ralf-pec-write-mcp-broker.service").read_text(encoding="utf-8")
    assert "WorkingDirectory=/home/sibilla-cumana/ralfloop-production/current" in unit
    assert "User=sibilla-cumana" in unit
    assert "Group=ralf-mcp" in unit
    assert "RuntimeDirectoryMode=0770" in unit
    assert "LoadCredential=pec-password:" in unit
    assert "BOTTAZZI_PEC_SMTP_PASSWORD_FILE=%d/pec-password" in unit
    assert "BOTTAZZI_PEC_WRITE_ATTACHMENT_ROOTS=/var/lib/ralfloop/pec-outbox" in unit
    assert "/run/ralf-pec-write-mcp/mcp.sock" in unit
    assert "--allow-group ralf-mcp" in unit
    assert "ralf_pec_write_mcp_server.py" in unit
    assert "smtps.pec.aruba.it" in unit
