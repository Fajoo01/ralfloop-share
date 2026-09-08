from datetime import datetime, timezone
from pathlib import Path

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
)
from ralfloop_agent.domains.domain_approval_store import (
    DomainApprovalStore,
)
from ralfloop_agent.unified_assistant.conversation import (
    ConversationManager,
)
from ralfloop_agent.unified_assistant.pec_runts import (
    RuntsAttachment,
    RuntsMessage,
    RuntsPractice,
)
from ralfloop_agent.unified_assistant.platform import SourceRef
from ralfloop_agent.unified_assistant.runts_write import (
    RUNTS_REPLY_ACTION,
    RuntsApprovedReplyExecutor,
    UnifiedRuntsApprovalCoordinator,
    build_runts_reply_approval_scope,
)
from ralfloop_agent.unified_assistant.runts_browser_write import (
    RuntsBrowserWriteError,
)
from ralfloop_agent.unified_assistant.contracts import PolicyClass


NOW = datetime(2026, 9, 7, tzinfo=timezone.utc)


def source(kind, ident, digest):
    return SourceRef(
        system="runts",
        native_id=ident,
        locator=f"synthetic://runts/{kind}/{ident}",
        observed_at=NOW.isoformat(),
        content_hash=digest,
    )


class Reader:
    def __init__(self, *, sent=False):
        self.sent = sent

    def get_practice(self, native_id):
        return RuntsPractice(
            native_id=native_id,
            status_raw="TRA",
            title="Synthetic",
            updated_at=NOW,
            observed_at=NOW,
            action_required=False,
            source=source("practice", native_id, "b" * 64),
            content_hash="c" * 64,
        )

    def list_messages(self, practice_id, *, limit):
        auth = RuntsMessage.build(
            native_id="523278",
            practice_id=practice_id,
            subject="Ufficio",
            body="Integrare Modello D",
            published_at=NOW,
            observed_at=NOW,
            attachments=(),
            source=source(
                "message",
                "523278",
                "a" * 64,
            ),
        )

        if not self.sent:
            return (auth,)

        attachment = RuntsAttachment(
            native_id="att-1",
            name="model-d.pdf",
            document_type="B00",
            content_hash=None,
            source=source(
                "attachment",
                "att-1",
                "d" * 64,
            ),
        )

        sent = RuntsMessage.build(
            native_id="900001",
            practice_id=practice_id,
            subject=PAYLOAD["subject"],
            body=PAYLOAD["body"],
            published_at=NOW,
            observed_at=NOW,
            attachments=(attachment,),
            source=source(
                "message",
                "900001",
                "e" * 64,
            ),
        )

        return (auth, sent)

    def get_message(self, native_id):
        return self.list_messages(
            "2603942",
            limit=100,
        )[0]


class Writer:
    def __init__(self, state):
        self.state = state

    def preflight(self, scope):
        self.state["preflight"] += 1

        return {
            "ok": True,
            "document_type_code": "B00",
        }

    def execute(self, scope):
        self.state["execute"] += 1
        self.state["sent"] = True

        return {
            "status": "provider_submit_ok",
            "writes": 2,
            "upload_document_id": "att-1",
        }


class FailingWriter(Writer):
    def __init__(
        self,
        state,
        *,
        phase,
        writes,
    ):
        super().__init__(state)
        self.phase = phase
        self.writes = writes

    def execute(self, scope):
        self.state["execute"] += 1

        raise RuntsBrowserWriteError(
            "runts_provider_write_timeout",
            phase=self.phase,
            writes=self.writes,
        )


def policy(tmp_path):
    return DomainApprovalPolicy(
        enabled=True,
        ttl_sec=3600,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        require_private_chat=True,
        db_path=str(tmp_path / "approval.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )


def make_pending(tmp_path):
    pdf = tmp_path / "model-d.pdf"
    pdf.write_bytes(b"%PDF-1.7\nsynthetic\n")

    import hashlib

    pdf_sha = hashlib.sha256(
        pdf.read_bytes()
    ).hexdigest()

    global PAYLOAD

    PAYLOAD = {
        "practice_id": "2603942",
        "authoritative_message_id": "523278",
        "authoritative_message_hash": "a" * 64,
        "proposal_id":
            "proposal.runts.document."
            + "1" * 24,
        "expected_practice_status": "TRA",
        "exercise": 2025,
        "subject":
            "Integrazione pratica 2603942 - "
            "Rendiconto per cassa 2025",
        "body":
            "Corpo esatto approvato per la risposta RUNTS.",
        "body_sha256": hashlib.sha256(
            b"Corpo esatto approvato per la risposta RUNTS."
        ).hexdigest(),
        "pdf_path": str(pdf),
        "pdf_name": pdf.name,
        "pdf_sha256": pdf_sha,
        "document_type_code": "B00",
        "channel": "RUNTS MESSAGGISTICA",
        "no_new_deposit": True,
    }

    manager = ConversationManager()

    pending = manager.stage(
        domain="runts",
        action=RUNTS_REPLY_ACTION,
        policy=PolicyClass.CONFIRM_WRITE,
        payload=PAYLOAD,
        displayed_text="RUNTS approval test",
    )

    return manager, pending


def approve(tmp_path):
    manager, pending = make_pending(tmp_path)
    pol = policy(tmp_path)
    store = DomainApprovalStore(policy=pol)

    coordinator = UnifiedRuntsApprovalCoordinator(
        store,
        policy=pol,
    )

    request = coordinator.request(
        pending,
        requested_by="test",
    )

    assert request["status"] == "pending"

    pending = manager.attach_approval_request(
        domain="runts",
        pending_id=pending.pending_id,
        payload_digest=pending.payload_digest,
        approval_ref=request["request_id"],
        created_at=request["created_at"],
        expires_at=request["expires_at"],
    )

    decision = coordinator.approve(
        pending,
        telegram_user_id=11,
        telegram_chat_id=22,
        telegram_message_id=33,
    )

    assert decision["status"] == "approved"

    pending = manager.bind_approval(
        domain="runts",
        pending_id=pending.pending_id,
        payload_digest=pending.payload_digest,
        approval_ref=pending.approval_ref,
    )

    return store, pending


def test_runts_scope_is_hash_bound(tmp_path):
    store, pending = approve(tmp_path)

    scope = build_runts_reply_approval_scope(
        pending
    )

    row = store.get_request(
        pending.approval_ref
    )

    assert row["scope"]["pdf_sha256"] == (
        scope["pdf_sha256"]
    )

    assert row["scope"]["practice_id"] == (
        "2603942"
    )

    assert row["scope"][
        "authoritative_message_id"
    ] == "523278"


def test_runts_write_disabled_by_default(tmp_path):
    store, pending = approve(tmp_path)

    calls = {
        "preflight": 0,
        "execute": 0,
        "sent": False,
    }

    executor = RuntsApprovedReplyExecutor(
        store=store,
        reader_factory=lambda: Reader(),
        writer_factory=lambda: Writer(calls),
        write_enabled=False,
    )

    result = executor.execute(pending)

    assert result["status"] == (
        "runts_write_disabled"
    )

    assert result["writes"] == 0
    assert calls["preflight"] == 0
    assert calls["execute"] == 0


def test_runts_exact_approved_execution_is_one_shot(
    tmp_path,
):
    store, pending = approve(tmp_path)

    state = {
        "preflight": 0,
        "execute": 0,
        "sent": False,
    }

    def reader():
        return Reader(sent=state["sent"])

    executor = RuntsApprovedReplyExecutor(
        store=store,
        reader_factory=reader,
        writer_factory=lambda: Writer(state),
        write_enabled=True,
    )

    result = executor.execute(pending)

    assert result["status"] == (
        "EXECUTED_VERIFIED"
    )

    assert result["executed"] is True
    assert state["execute"] == 1

    second = executor.execute(pending)

    assert second["status"] == (
        "already_executed"
    )

    assert state["execute"] == 1


def test_runts_pdf_drift_fails_before_write(
    tmp_path,
):
    store, pending = approve(tmp_path)

    Path(
        pending.payload["pdf_path"]
    ).write_bytes(b"changed")

    calls = {
        "preflight": 0,
        "execute": 0,
        "sent": False,
    }

    executor = RuntsApprovedReplyExecutor(
        store=store,
        reader_factory=lambda: Reader(),
        writer_factory=lambda: Writer(calls),
        write_enabled=True,
    )

    result = executor.execute(pending)

    assert result["status"] == (
        "FAILED_BEFORE_WRITE"
    )

    assert result["writes"] == 0
    assert calls["execute"] == 0

def test_runts_upload_wait_timeout_reports_zero_confirmed_writes(
    tmp_path,
):
    store, pending = approve(tmp_path)

    state = {
        "preflight": 0,
        "execute": 0,
        "sent": False,
    }

    executor = RuntsApprovedReplyExecutor(
        store=store,
        reader_factory=lambda: Reader(),
        writer_factory=lambda: FailingWriter(
            state,
            phase="upload_wait",
            writes=0,
        ),
        write_enabled=True,
    )

    result = executor.execute(pending)

    assert result["status"] == "EXECUTION_UNCERTAIN"
    assert result["executed"] is False
    assert result["retry_allowed"] is False

    assert result["writes"] == 0
    assert result["phase"] == "upload_wait"
    assert result["reason"] == (
        "runts_provider_write_timeout"
    )
    assert result["error_type"] == (
        "RuntsBrowserWriteError"
    )

    assert state["preflight"] == 1
    assert state["execute"] == 1


def test_runts_send_wait_timeout_reports_one_confirmed_write(
    tmp_path,
):
    store, pending = approve(tmp_path)

    state = {
        "preflight": 0,
        "execute": 0,
        "sent": False,
    }

    executor = RuntsApprovedReplyExecutor(
        store=store,
        reader_factory=lambda: Reader(),
        writer_factory=lambda: FailingWriter(
            state,
            phase="send_wait",
            writes=1,
        ),
        write_enabled=True,
    )

    result = executor.execute(pending)

    assert result["status"] == "EXECUTION_UNCERTAIN"
    assert result["executed"] is False
    assert result["retry_allowed"] is False

    assert result["writes"] == 1
    assert result["phase"] == "send_wait"
    assert result["reason"] == (
        "runts_provider_write_timeout"
    )
    assert result["error_type"] == (
        "RuntsBrowserWriteError"
    )

    assert state["preflight"] == 1
    assert state["execute"] == 1
