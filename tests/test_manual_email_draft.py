"""No-network tests for the protected Bot-tazzi Gmail draft-only route."""
from pathlib import Path
import hashlib

import pytest

from openshell_backend.manual_email_draft import create_manual_draft, DraftPreparationError

BASE = Path("/home/bandi/bottazzi-email-exactbody-fix-20261008/docs/perrifarci/email_drafts")
WORKSPACE = Path("/home/bandi/.local/share/google-workspace-mcp/workspace")


class FakeSync:
    def __init__(self):
        self.calls = []

    def upsert(self, **kwargs):
        self.calls.append(kwargs)
        return {"draftId":"canary-draft-123","rawSha256":"c"*64, "sent":False}


def data(name, to):
    attachments=[]
    for ext in (".docx",".pdf"):
        p=WORKSPACE / f"A2_ARCI_07ott2026_{name}_DA_FIRMARE{ext}"
        attachments.append({"path":str(p),"name":p.name,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()})
    return (
        f"Prepara una email di rettifica a {to}. Destinatario verificato: {to}. "
        "Il nuovo Allegato A2 Educativa per PerRifArci prevede cofinanziamento € 0,00. "
        "Riconosci esplicitamente i nostri errori precedenti. Solo BOZZA GMAIL: non inviare.",
        {"source":"ralf_terminal","email_manual_draft_only":True,
         "email_exact_body":(BASE / (name+".txt")).read_text(),
         "email_exact_subject":"PerRifArci – scuse e Allegato A2 corretto",
         "email_attachments":attachments},
    )


@pytest.mark.parametrize("name,to",[("PiGreco","pigreco@luogoideale.org"),("Amici_Parco_Trotter","amicitrotter@gmail.com"),("AMEDI","info@amedi.it")])
def test_only_gmail_draft_with_correct_two_attachments(name,to):
    message,context=data(name,to)
    fake=FakeSync()
    result=create_manual_draft(message,context,sync_factory=lambda:fake)
    assert result["status"] == "draft_only" and result["send_calls"] == 0 and not result["sent"]
    assert result["gmail_draft_id"] == "canary-draft-123"
    assert fake.calls[0]["to"] == to
    assert fake.calls[0]["body"] == context["email_exact_body"].strip()
    assert len(fake.calls[0]["attachments"]) == 2


def test_wrong_attachment_sha_fails_closed():
    msg,ctx=data("AMEDI","info@amedi.it")
    ctx["email_attachments"][0]["sha256"]="0"*64
    with pytest.raises(DraftPreparationError,match="attachment_hash_mismatch"):
        create_manual_draft(msg,ctx,sync_factory=FakeSync)


def test_no_draft_only_flag_fails_closed():
    msg,ctx=data("AMEDI","info@amedi.it")
    del ctx["email_manual_draft_only"]
    with pytest.raises(DraftPreparationError,match="manual_draft_only_required"):
        create_manual_draft(msg,ctx,sync_factory=FakeSync)


def test_unverified_address_fails_closed():
    msg,ctx=data("AMEDI","info@amedi.it")
    msg=msg.replace("Destinatario verificato: info@amedi.it","Destinatario verificato: sconosciuto")
    with pytest.raises(DraftPreparationError,match="verified_recipient_required"):
        create_manual_draft(msg,ctx,sync_factory=FakeSync)
