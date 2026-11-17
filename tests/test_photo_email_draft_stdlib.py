"""Dependency-free Forgejo CI contract for the real photo draft module."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "openshell_backend/photo_email_draft.py"

def load_module():
    for key in ("ralfloop_agent", "ralfloop_agent.unified_assistant"):
        stub = types.ModuleType(key)
        stub.__path__ = []
        sys.modules[key] = stub
    classes = {
        "email": {"EmailWorkingMemoryBuilder": type("Builder", (), {
            "__init__": lambda self, *a, **kw: None,
            "build": lambda self, **kw: kw})},
        "email_pipeline": {"GenericEmailPipeline": type("Pipeline", (), {
            "validate_exact": lambda self, w, b: types.SimpleNamespace(
                hard_guard="passed", final_validator="passed")})},
        "memory": {"MemoryRouter": type("MemoryRouter", (), {})},
        "registry": {"UnifiedRegistryFacade": type("Registry", (), {
            "domain": lambda self, n: n})},
    }
    for name, members in classes.items():
        mod = types.ModuleType("ralfloop_agent.unified_assistant." + name)
        mod.__dict__.update(members)
        sys.modules[mod.__name__] = mod
    spec = importlib.util.spec_from_file_location("photo_draft_standalone", SOURCE)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj


class TestPhotoDraft(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mod = load_module()
        self.mod.WORKSPACE = Path(self.tmp.name)
        p = self.mod.WORKSPACE / "evidence.jpg"
        p.write_bytes(b"\xff\xd8\xff" + b"authentic draft fixture" + b"\xff\xd9")
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        self.ctx = {
            "source": "ralf_terminal", "email_manual_draft_only": True,
            "email_exact_subject": "Evidence photo", "email_exact_body": "Confirmed text.",
            "email_attachments": [{"path": str(p), "name": p.name, "sha256": digest}]}
        self.msg = "Foto verificata: evidence.jpg. Destinatario verificato: example@example.com."
        self.calls = []
        calls = self.calls
        class FakeSync:
            def upsert(self, **kwargs):
                calls.append(kwargs)
                return {"draftId": kwargs.get("draft_id") or "r-456789123456789", "sent": False,
                        "rawSha256": "a"*64}
        self.factory = FakeSync

    def test_create_draft_not_send(self):
        out = self.mod.create_photo_draft(self.msg, self.ctx, "example@example.com", self.factory)
        self.assertFalse(out["sent"])
        self.assertEqual(out["send_calls"], 0)
        self.assertEqual(self.calls[0]["attachments"][0]["name"], "evidence.jpg")

    def test_existing_id_is_never_put_updated(self):
        draft = "r-123456789"
        before = "a"*64
        self.ctx.update(email_existing_draft_id=draft, email_existing_draft_raw_sha256=before)
        self.msg += " Bozza verificata: " + draft + ". SHA256 MIME precedente: " + before
        out = self.mod.create_photo_draft(self.msg, self.ctx, "example@example.com", self.factory)
        self.assertNotEqual(out["gmail_draft_id"], draft)
        self.assertEqual(out["replacement_of"], draft)
        self.assertTrue(out["previous_draft_retained"])
        self.assertNotIn("draft_id", self.calls[0])

    def test_bad_digest_rejected_without_write(self):
        self.ctx["email_attachments"][0]["sha256"] = "0"*64
        with self.assertRaisesRegex(self.mod.DraftPreparationError, "photo_hash_mismatch"):
            self.mod.create_photo_draft(self.msg, self.ctx, "example@example.com", self.factory)
        self.assertEqual(self.calls, [])

    def test_wrong_existing_proof_rejected(self):
        self.ctx.update(email_existing_draft_id="r-999999999",
                        email_existing_draft_raw_sha256="a"*64)
        with self.assertRaisesRegex(self.mod.DraftPreparationError, "photo_update_proof_not_in_request"):
            self.mod.create_photo_draft(self.msg, self.ctx, "example@example.com", self.factory)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
