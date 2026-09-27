from datetime import datetime, timezone

from src.outbound_approval import (
    OutboundApproval,
    email_action_digest,
    message_action_digest,
    print_action_digest,
    validate_outbound_approval,
)


def _approval(digest: str, *, channel: str = "telegram", approved_by: str = "fabio") -> OutboundApproval:
    return OutboundApproval(
        action_digest=digest,
        approved_by=approved_by,
        channel=channel,  # type: ignore[arg-type]
        approved_at="2026-09-27T14:00:00+00:00",
        reference="msg-123",
    )


def _validate(approval, digest):
    return validate_outbound_approval(
        approval,
        expected_digest=digest,
        fabio_identities={"fabio"},
        now=datetime(2026, 9, 27, 14, 1, tzinfo=timezone.utc),
    )


def test_missing_approval_is_denied():
    ok, reason = _validate(None, "x")
    assert not ok
    assert reason == "missing_approval"


def test_print_exact_action_is_allowed():
    digest = print_action_digest(document_id="doc.pdf", printer="hp", copies=1)
    assert _validate(_approval(digest), digest) == (True, "approved")


def test_email_exact_action_is_allowed():
    digest = email_action_digest(recipients=["dest@example.org"], subject="S", body="B")
    assert _validate(_approval(digest, channel="whatsapp"), digest) == (True, "approved")


def test_message_exact_action_is_allowed():
    digest = message_action_digest(channel="whatsapp", recipients=["fabio"], body="ciao")
    assert _validate(_approval(digest), digest) == (True, "approved")


def test_email_change_recipient_requires_new_approval():
    approved = email_action_digest(recipients=["a@example.org"], subject="S", body="B")
    changed = email_action_digest(recipients=["b@example.org"], subject="S", body="B")
    ok, reason = _validate(_approval(approved), changed)
    assert not ok
    assert reason == "approval_action_mismatch"


def test_email_change_body_requires_new_approval():
    approved = email_action_digest(recipients=["a@example.org"], subject="S", body="prima")
    changed = email_action_digest(recipients=["a@example.org"], subject="S", body="dopo")
    ok, reason = _validate(_approval(approved), changed)
    assert not ok
    assert reason == "approval_action_mismatch"


def test_message_change_attachment_requires_new_approval():
    approved = message_action_digest(channel="telegram", recipients=["x"], body="ok", attachments=["a.pdf"])
    changed = message_action_digest(channel="telegram", recipients=["x"], body="ok", attachments=["b.pdf"])
    ok, reason = _validate(_approval(approved), changed)
    assert not ok
    assert reason == "approval_action_mismatch"


def test_other_person_is_denied():
    digest = message_action_digest(channel="telegram", recipients=["x"], body="ok")
    ok, reason = _validate(_approval(digest, approved_by="other"), digest)
    assert not ok
    assert reason == "approver_not_fabio"


def test_stale_approval_is_denied():
    digest = email_action_digest(recipients=["a@example.org"], subject="S", body="B")
    ok, reason = validate_outbound_approval(
        _approval(digest),
        expected_digest=digest,
        fabio_identities={"fabio"},
        max_age_seconds=60,
        now=datetime(2026, 9, 27, 15, 2, tzinfo=timezone.utc),
    )
    assert not ok
    assert reason == "approval_stale"
