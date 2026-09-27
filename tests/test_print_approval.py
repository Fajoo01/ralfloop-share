from datetime import datetime, timezone

from src.print_approval import PrintApproval, print_job_digest, validate_print_approval


def _approval(digest: str, *, channel: str = "telegram", approved_by: str = "fabio") -> PrintApproval:
    return PrintApproval(
        job_digest=digest,
        approved_by=approved_by,
        channel=channel,  # type: ignore[arg-type]
        approved_at="2026-09-27T14:00:00+00:00",
        reference="msg-123",
    )


def test_missing_approval_is_denied():
    ok, reason = validate_print_approval(
        None,
        expected_digest="x",
        fabio_identities={"fabio"},
        now=datetime(2026, 9, 27, 14, 1, tzinfo=timezone.utc),
    )
    assert not ok
    assert reason == "missing_approval"


def test_telegram_approval_for_exact_job_is_allowed():
    digest = print_job_digest(document_id="doc.pdf", printer="hp", copies=1)
    ok, reason = validate_print_approval(
        _approval(digest, channel="telegram"),
        expected_digest=digest,
        fabio_identities={"fabio"},
        now=datetime(2026, 9, 27, 14, 1, tzinfo=timezone.utc),
    )
    assert ok
    assert reason == "approved"


def test_whatsapp_approval_for_exact_job_is_allowed():
    digest = print_job_digest(document_id="doc.pdf", printer="hp", copies=1)
    ok, _ = validate_print_approval(
        _approval(digest, channel="whatsapp"),
        expected_digest=digest,
        fabio_identities={"fabio"},
        now=datetime(2026, 9, 27, 14, 1, tzinfo=timezone.utc),
    )
    assert ok


def test_other_person_is_denied():
    digest = print_job_digest(document_id="doc.pdf", printer="hp", copies=1)
    ok, reason = validate_print_approval(
        _approval(digest, approved_by="other"),
        expected_digest=digest,
        fabio_identities={"fabio"},
        now=datetime(2026, 9, 27, 14, 1, tzinfo=timezone.utc),
    )
    assert not ok
    assert reason == "approver_not_fabio"


def test_approval_cannot_be_reused_for_changed_job():
    old_digest = print_job_digest(document_id="doc.pdf", printer="hp", copies=1)
    new_digest = print_job_digest(document_id="doc.pdf", printer="hp", copies=5)
    ok, reason = validate_print_approval(
        _approval(old_digest),
        expected_digest=new_digest,
        fabio_identities={"fabio"},
        now=datetime(2026, 9, 27, 14, 1, tzinfo=timezone.utc),
    )
    assert not ok
    assert reason == "approval_job_mismatch"


def test_stale_approval_is_denied():
    digest = print_job_digest(document_id="doc.pdf", printer="hp", copies=1)
    ok, reason = validate_print_approval(
        _approval(digest),
        expected_digest=digest,
        fabio_identities={"fabio"},
        max_age_seconds=60,
        now=datetime(2026, 9, 27, 15, 2, tzinfo=timezone.utc),
    )
    assert not ok
    assert reason == "approval_stale"
