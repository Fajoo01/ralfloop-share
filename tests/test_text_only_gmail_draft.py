from openshell_backend.manual_email_draft import create_text_only_draft, DraftPreparationError
import pytest

class FakeSync:
    def __init__(self):
        self.calls = []
    def upsert(self, **kwargs):
        self.calls.append(kwargs)
        return {"draftId":"draft-jarach-123", "rawSha256":"a"*64, "sent":False}

def request():
    to = "sindaco.sala@comune.milano.it"
    cc = "m.municipio2@comune.milano.it,presidentelocatelli.municipio2@comune.milano.it"
    message = f"Scrivi una mail a {to}. Destinatario verificato: {to}. Cc verificata: {cc}. Prepara solo bozza, non inviare."
    context = {"source":"ralf_terminal", "email_manual_draft_only": True,
               "email_exact_body":"Al Sindaco, segnaliamo da anni ristagni d'acqua in via Jarach e una pulizia insufficiente. Chiediamo una caditoia nuova e una risposta scritta. Cordiali saluti, Tiremm Innanz APS.",
               "email_exact_subject":"Via Jarach – caditoia mancante e pulizia insufficiente",
               "email_exact_cc":cc}
    return message, context

def test_text_only_draft_no_send_and_verified_cc():
    message, context = request()
    sync = FakeSync()
    result = create_text_only_draft(message, context, sync_factory=lambda: sync)
    assert result["gmail_draft_id"] == "draft-jarach-123" and result["sent"] is False
    assert result["send_calls"] == 0 and result["attachment_names"] == []
    assert sync.calls[0]["attachments"] == ()
    assert sync.calls[0]["cc"] == context["email_exact_cc"]

def test_missing_verified_cc_blocks_without_write():
    message, context = request()
    sync = FakeSync()
    context["email_exact_cc"] = "other@example.org"
    with pytest.raises(DraftPreparationError, match="text_only_cc_unverified"):
        create_text_only_draft(message, context, sync_factory=lambda: sync)
    assert not sync.calls

def test_attachments_and_missing_draft_policy_block_without_write():
    message, context = request()
    sync = FakeSync()
    context["email_attachments"] = [{"name":"wrong.pdf"}]
    with pytest.raises(DraftPreparationError, match="text_only_attachments_denied"):
        create_text_only_draft(message, context, sync_factory=lambda: sync)
    context.pop("email_attachments")
    context.pop("email_manual_draft_only")
    with pytest.raises(DraftPreparationError, match="manual_draft_only_required"):
        create_text_only_draft(message, context, sync_factory=lambda: sync)
    assert not sync.calls
