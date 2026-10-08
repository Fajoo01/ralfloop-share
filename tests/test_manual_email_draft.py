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

def arci_bundle():
    names=[
        "A2_ARCI_07ott2026_PiGreco_signed.pdf",
        "A2_ARCI_07ott2026_AMEDI_FIRMATA.docx",
        "CI Irene Pani fronte.jpg",
        "CI Irene Pani retro.jpg",
    ]
    attachments=[]
    for name in names:
        p=WORKSPACE/name
        attachments.append({"path":str(p),"name":name,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()})
    return (
        "Prepara una email a Irene e Giulio per PerRifArci. "
        "Destinatario verificato: irene.conca@arcimilano.it. "
        "Cc verificata: franchini.giulio@gmail.com. "
        "PiGreco ha A2 firmato ma manca documento d'identità. "
        "AMEDI ha A2 firmato e carta identità fronte retro. "
        "Trotter ha inviato lettera generica non A2, scuole lente. "
        "Solo BOZZA GMAIL, non inviare.",
        {"source":"ralf_terminal","email_manual_draft_only":True,
         "email_support_bundle":True,
         "email_exact_cc":"franchini.giulio@gmail.com",
         "email_exact_subject":"PerRifArci – lettere A2 ricevute e aggiornamento scuole",
         "email_exact_body":(BASE/"ARCI_Irene_Giulio.txt").read_text(),
         "email_attachments":attachments},
    )


def test_arci_bundle_draft_only_verified_cc_and_four_files():
    msg,ctx=arci_bundle()
    fake=FakeSync()
    r=create_manual_draft(msg,ctx,sync_factory=lambda:fake)
    assert r["status"]=="draft_only" and r["sent"] is False
    assert fake.calls[0]["to"]=="irene.conca@arcimilano.it"
    assert fake.calls[0]["cc"]=="franchini.giulio@gmail.com"
    assert len(fake.calls[0]["attachments"])==4


def test_arci_bulk_requires_exact_recipients():
    msg,ctx=arci_bundle()
    ctx["email_exact_cc"]="wrong@example.org"
    with pytest.raises(DraftPreparationError,match="arci_recipients_unverified"):
        create_manual_draft(msg,ctx,sync_factory=FakeSync)


def test_arci_bulk_must_not_attach_wrong_letter():
    msg,ctx=arci_bundle()
    path=WORKSPACE/"Arci.pdf"
    ctx["email_attachments"][0]={"path":str(path),"name":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    with pytest.raises(DraftPreparationError,match="arci_bundle_files_invalid"):
        create_manual_draft(msg,ctx,sync_factory=FakeSync)


def test_giorgio_trotter_reply_stays_in_verified_thread_and_draft_only():
    msg,ctx=data("Amici_Parco_Trotter","amicitrotter@gmail.com")
    msg += " Thread verificato: 1a11b26d538e3ca8."
    ctx["email_reply_thread_id"]="1a11b26d538e3ca8"
    ctx["email_exact_subject"]="Re: PerRifArci – scuse e Allegato A2 corretto per Amici del Trotter"
    ctx["email_exact_body"]=(BASE/"Giorgio_Trotter_solleticito_A2_regione_20261008.txt").read_text()
    fake=FakeSync()
    result=create_manual_draft(msg,ctx,sync_factory=lambda:fake)
    assert result["gmail_thread_id"]=="1a11b26d538e3ca8"
    assert fake.calls[0]["thread_id"]=="1a11b26d538e3ca8"
    assert len(fake.calls[0]["attachments"])==2
    assert result["sent"] is False


def test_giorgio_wrong_thread_must_fail_closed():
    msg,ctx=data("Amici_Parco_Trotter","amicitrotter@gmail.com")
    ctx["email_reply_thread_id"]="1a11b26d538e3ca9"
    with pytest.raises(DraftPreparationError,match="reply_thread_unverified"):
        create_manual_draft(msg,ctx,sync_factory=FakeSync)
