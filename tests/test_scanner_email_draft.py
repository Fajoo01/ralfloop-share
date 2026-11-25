from pathlib import Path
from types import SimpleNamespace
import hashlib
import pytest
from openshell_backend import scanner_email_draft as s
from openshell_backend.manual_email_draft import create_manual_draft

@pytest.fixture
def sample(tmp_path, monkeypatch):
    monkeypatch.setattr(s,"WORKSPACE",tmp_path)
    monkeypatch.setattr(s,"STATE",tmp_path/"state")
    p=tmp_path/"Scansione_Epson_2026-11-25.pdf"
    p.write_bytes(b"%PDF-1.4\nfixture")
    sha=hashlib.sha256(p.read_bytes()).hexdigest()
    monkeypatch.setattr(s.subprocess,"run",lambda *a,**k:SimpleNamespace(stdout="Pages: 1\n"))
    monkeypatch.setattr(s,"provider_read",lambda op,**k:"No messages found for query:" if op=="search" else "r-123456789")
    msg=f"Prepara una mail solo in bozza. Destinatario verificato: sonia@example.com. PDF verificato: {p.name}. SHA256 PDF: {sha}"
    ctx={"source":"ralf_terminal","email_manual_draft_only":True,"email_scanner_pdf_draft":True,
         "email_exact_subject":"Documento scansionato","email_exact_body":"Ciao Sonia, ti mando il documento scansionato in sede.",
         "email_attachments":[{"path":str(p),"name":p.name,"sha256":sha}]}
    calls=[]
    def create(**kw):
        calls.append(kw)
        return {"draftId":"r-123456789","rawSha256":"a"*64,"sent":False}
    return msg,ctx,p,calls,lambda:SimpleNamespace(upsert=create)

def test_create_and_reuse(sample):
    msg,ctx,p,calls,factory=sample
    first=create_manual_draft(msg,ctx,sync_factory=factory)
    again=create_manual_draft(msg,ctx,sync_factory=factory)
    assert first["pages"]==1 and first["sent"] is False and first["send_calls"]==0
    assert again["reused"] and again["writes"]==0 and len(calls)==1
    assert first["gmail_draft_id"]==again["gmail_draft_id"]

def test_modified_pdf_fails(sample):
    msg,ctx,p,calls,factory=sample
    p.write_bytes(b"%PDF-1.4\nmodified")
    with pytest.raises(s.DraftPreparationError,match="hash_or_signature"):
        create_manual_draft(msg,ctx,sync_factory=factory)
    assert not calls

def test_no_draft_flag(sample):
    msg,ctx,p,calls,factory=sample
    ctx["email_manual_draft_only"]=False
    with pytest.raises(s.DraftPreparationError):
        create_manual_draft(msg,ctx,sync_factory=factory)
    assert not calls

def test_uncertain_create_no_retry(sample):
    msg,ctx,p,calls,factory=sample
    def broken(**kw):
        calls.append(kw)
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        create_manual_draft(msg,ctx,sync_factory=lambda:SimpleNamespace(upsert=broken))
    with pytest.raises(s.DraftPreparationError,match="outcome_uncertain"):
        create_manual_draft(msg,ctx,sync_factory=factory)
    assert len(calls)==1

def test_existing_remote_draft_blocks_create(sample,monkeypatch):
    msg,ctx,p,calls,factory=sample
    monkeypatch.setattr(s,"provider_read",lambda *a,**k:"Found an existing draft")
    with pytest.raises(s.DraftPreparationError,match="existing_or_search_uncertain"):
        create_manual_draft(msg,ctx,sync_factory=factory)
    assert not calls
