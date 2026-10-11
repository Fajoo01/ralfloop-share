import hashlib
from pathlib import Path

import pytest
from openshell_backend.manual_email_draft import create_manual_draft, DraftPreparationError

WORKSPACE = Path("/home/bandi/.local/share/google-workspace-mcp/workspace")
NAMES = ("01_Iscrizione_corso_cinema_Trotter.pdf", "02_Domanda_ammissione_Tiremm_ARCI_minori.pdf")
TO = "acagnoprod@gmail.com"
SUBJECT = "Corso di cinema al Trotter – moduli di iscrizione e tesseramento Tiremm"

class DraftRecorder:
    def __init__(self):
        self.writes = []
    def upsert(self, **kwargs):
        self.writes.append(kwargs)
        return {"draftId": "test-cinema-only", "rawSha256": "a" * 64, "sent": False}

def request(recipient=TO):
    files = [{"path":str(WORKSPACE / n), "name":n,
              "sha256":hashlib.sha256((WORKSPACE / n).read_bytes()).hexdigest()} for n in NAMES]
    msg = f"Scrivi una mail a {recipient}. Destinatario verificato: {recipient}. Prepara solo bozza, non inviare."
    ctx = {"source":"ralf_terminal", "email_manual_draft_only":True,
           "email_exact_subject":SUBJECT,
           "email_exact_body":"Ciao Andrea, in allegato trovi i due moduli per il corso di cinematografia al Parco Trotter. Ti chiedo di distribuirli alle famiglie e di farceli restituire compilati. Grazie, Fabio — Tiremm Innanz APS.",
           "email_attachments":files}
    return msg, ctx

def test_exact_cinema_pair_is_draft_only():
    m,c=request()
    fake=DraftRecorder()
    result=create_manual_draft(m,c,sync_factory=lambda:fake)
    assert result["sent"] is False and result["send_calls"]==0
    assert result["attachment_names"]==list(NAMES)
    assert len(fake.writes)==1 and len(fake.writes[0]["attachments"])==2

def test_wrong_recipient_denied():
    m,c=request("other@example.org")
    fake=DraftRecorder()
    with pytest.raises(DraftPreparationError,match="word_and_pdf_required"):
        create_manual_draft(m,c,sync_factory=lambda:fake)
    assert not fake.writes

def test_tampered_hash_denied():
    m,c=request()
    c["email_attachments"][0]["sha256"]="0"*64
    fake=DraftRecorder()
    with pytest.raises(DraftPreparationError,match="attachment_hash_mismatch"):
        create_manual_draft(m,c,sync_factory=lambda:fake)
    assert not fake.writes
