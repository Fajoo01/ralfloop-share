"""Credential-free CI: exercise real scanner draft contract with provider doubles."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from test_photo_email_draft_stdlib import load_module

class ScannerDraftContract(unittest.TestCase):
    def setUp(self):
        photo=load_module()
        package=types.ModuleType("openshell_backend")
        package.__path__=[]
        transport=types.ModuleType("src.mcp_transport")
        transport.MCPClientSession=transport.UnixMCPTransport=object
        self.modules=patch.dict(sys.modules,{"openshell_backend":package,
            "openshell_backend.photo_email_draft":photo,"src.mcp_transport":transport})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        path=Path(__file__).resolve().parents[1]/"openshell_backend/scanner_email_draft.py"
        spec=importlib.util.spec_from_file_location("openshell_backend.scanner_email_draft",path)
        self.mod=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.mod)
        t=tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.mod.WORKSPACE=Path(t.name)
        self.mod.STATE=Path(t.name)/"state"
        self.pdf=Path(t.name)/"Scansione_Epson_2026-11-25.pdf"
        self.pdf.write_bytes(b"%PDF-1.4\nfixture")
        sha=hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.ctx={"source":"ralf_terminal","email_manual_draft_only":True,
                  "email_exact_subject":"Documento","email_exact_body":"Testo verificato.",
                  "email_attachments":[{"path":str(self.pdf),"name":self.pdf.name,"sha256":sha}]}
        self.msg="PDF verificato: "+self.pdf.name+". SHA256 PDF: "+sha
        self.calls=[]
        p=patch.object(self.mod.subprocess,"run",return_value=types.SimpleNamespace(stdout="Pages: 1\n"))
        p.start()
        self.addCleanup(p.stop)
        self.mod.provider_read=lambda op,**kw:"No messages found for query:" if op=="search" else "r-123456789"
    def factory(self):
        def upsert(**kw):
            self.calls.append(kw)
            return {"draftId":"r-123456789","rawSha256":"a"*64,"sent":False}
        return types.SimpleNamespace(upsert=upsert)
    def create(self):
        return self.mod.create_scanner_draft(self.msg,self.ctx,"test@example.com",self.factory)
    def test_once(self):
        r=self.create()
        self.assertFalse(r["sent"])
        self.assertEqual(r["send_calls"],0)
        self.assertTrue(self.create()["reused"])
        self.assertEqual(len(self.calls),1)
    def test_changed_pdf(self):
        self.pdf.write_bytes(b"%PDF-1.4\nchanged")
        with self.assertRaises(self.mod.DraftPreparationError):
            self.create()
        self.assertEqual(self.calls,[])
    def test_uncertain_existing(self):
        self.mod.provider_read=lambda *a,**kw:"unverified result"
        with self.assertRaises(self.mod.DraftPreparationError):
            self.create()
        self.assertEqual(self.calls,[])
