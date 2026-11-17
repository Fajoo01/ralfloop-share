"""Offline contract: photo attachment is a protected DRAFT write, never send."""
from pathlib import Path
import hashlib
import pytest

import openshell_backend.photo_email_draft as photo
from openshell_backend.manual_email_draft import create_manual_draft, DraftPreparationError

ADDRESS = "sindaco.sala@comune.milano.it"
DRAFT = "r-994362734047347362"
OLD_RAW = "1"*64


class FakeSync:
    def __init__(self):
        self.calls = []

    def upsert(self, **kwargs):
        self.calls.append(kwargs)
        return {"draftId": kwargs.get("draft_id") or "r-123456789",
                "rawSha256": "a"*64, "sent": False}


def sample(tmp_path, monkeypatch, update=False):
    monkeypatch.setattr(photo, "WORKSPACE", tmp_path)
    pic = tmp_path / "photo.jpg"
    pic.write_bytes(b"\xff\xd8\xff" + b"test image pixels" + b"\xff\xd9")
    digest = hashlib.sha256(pic.read_bytes()).hexdigest()
    msg = ("Prepara una mail. Destinatario verificato: " + ADDRESS
           + ". Foto verificata: photo.jpg. Cc verificata: servizioclienti@amsa.it.")
    ctx = {"source": "ralf_terminal", "email_manual_draft_only": True,
           "email_photo_draft": True,
           "email_exact_subject": "Re: Foto dei bidoni via Asiago",
           "email_exact_body": "Buongiorno, i due bidoni sono ancora lì. Chiedo la rimozione. Cordiali saluti.",
           "email_exact_cc": "servizioclienti@amsa.it",
           "email_attachments": [{"path": str(pic), "name": pic.name, "sha256": digest}]}
    if update:
        ctx["email_existing_draft_id"] = DRAFT
        ctx["email_existing_draft_raw_sha256"] = OLD_RAW
        msg += " Bozza verificata: " + DRAFT + ". SHA256 MIME precedente: " + OLD_RAW
    return msg, ctx


def test_create_photo_only_draft_with_cc(tmp_path, monkeypatch):
    msg, ctx = sample(tmp_path, monkeypatch)
    fake = FakeSync()
    res = create_manual_draft(msg, ctx, sync_factory=lambda: fake)
    assert res["sent"] is False and res["send_calls"] == 0
    assert res["attachment_names"] == ["photo.jpg"]
    assert res["updated_existing"] is False
    assert fake.calls[0]["attachments"][0]["sha256"] == ctx["email_attachments"][0]["sha256"]
    assert fake.calls[0]["cc"] == "servizioclienti@amsa.it"


def test_single_photo_automatically_uses_safe_draft_route(tmp_path, monkeypatch):
    msg, ctx = sample(tmp_path, monkeypatch)
    ctx.pop("email_photo_draft")
    result = create_manual_draft(msg, ctx, sync_factory=FakeSync)
    assert result["attachment_names"] == ["photo.jpg"]
    assert result["sent"] is False


def test_replace_photo_draft_non_destructively(tmp_path, monkeypatch):
    msg, ctx = sample(tmp_path, monkeypatch, update=True)
    fake = FakeSync()
    res = create_manual_draft(msg, ctx, sync_factory=lambda: fake)
    assert res["updated_existing"] is False
    assert res["replacement_of"] == DRAFT
    assert res["previous_draft_retained"] is True
    assert res["gmail_draft_id"] != DRAFT
    assert fake.calls[0].get("draft_id") is None


@pytest.mark.parametrize("edit,error", [
    ("bad_digest", "photo_hash_mismatch"),
    ("wrong_cc", "photo_cc_not_verified"),
    ("missing_photo_marker", "photo_evidence_not_in_request"),
    ("bad_magic", "photo_mime_invalid"),
    ("outside", "photo_outside_workspace"),
    ("missing_update_proof", "photo_update_proof_not_in_request"),
    ("id_without_hash", "photo_update_proof_incomplete"),
    ("two_photos", "photo_count_invalid"),
])
def test_photo_fails_closed(tmp_path, monkeypatch, edit, error):
    msg, ctx = sample(tmp_path, monkeypatch, update=edit == "missing_update_proof")
    p = Path(ctx["email_attachments"][0]["path"])
    if edit == "bad_digest":
        ctx["email_attachments"][0]["sha256"] = "f"*64
    elif edit == "wrong_cc":
        msg = msg.replace("servizioclienti@amsa.it.", "nobody@example.net.")
    elif edit == "missing_photo_marker":
        msg = msg.replace("Foto verificata: photo.jpg.", "")
    elif edit == "bad_magic":
        p.write_bytes(b"notjpeg"*4)
        ctx["email_attachments"][0]["sha256"] = hashlib.sha256(p.read_bytes()).hexdigest()
    elif edit == "outside":
        ctx["email_attachments"][0]["path"] = "/tmp/photo.jpg"
    elif edit == "missing_update_proof":
        msg = msg.replace("Bozza verificata: " + DRAFT, "Bozza assente")
    elif edit == "id_without_hash":
        ctx["email_existing_draft_id"] = DRAFT
    elif edit == "two_photos":
        ctx["email_attachments"].append(ctx["email_attachments"][0])
    fake = FakeSync()
    with pytest.raises(DraftPreparationError, match=error):
        create_manual_draft(msg, ctx, sync_factory=lambda: fake)
    assert fake.calls == []


def test_update_wrong_id_fails_closed(tmp_path, monkeypatch):
    msg, ctx = sample(tmp_path, monkeypatch, update=True)
    ctx["email_existing_draft_id"] = "r-888888888888888888"
    fake = FakeSync()
    with pytest.raises(DraftPreparationError, match="photo_update_proof_not_in_request"):
        create_manual_draft(msg, ctx, sync_factory=lambda: fake)
    assert not fake.calls
