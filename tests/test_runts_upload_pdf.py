"""Local PDF/ACL contracts. No network transports or production artifacts."""
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

import pytest

from ralfloop_agent.unified_assistant import runts_upload_pdf as pdf
from ralfloop_agent.unified_assistant import runts_browser_probe as probe
from ralfloop_agent.unified_assistant.runts_write import build_runts_pending_payload


def proposal_for(path):
    return {"status": "READY_FOR_HUMAN_APPROVAL", "blockers": [],
        "review_context": {"pdf_path": str(path)}, "final_validation": {"exercise": 2025},
        "practice_id": "123", "authoritative_message_id": "456", "proposal_id": "synthetic",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "provenance": [{"system": "runts", "native_id": "456", "content_hash": "a" * 64}]}


def test_final_path_and_hash_bound_before_approval(tmp_path, monkeypatch):
    final = tmp_path / "final.pdf"
    final.write_bytes(b"final-pdfa")
    monkeypatch.setattr(pdf, "validate_pdfa", lambda _: None)
    monkeypatch.setattr(pdf, "verify_browser_readable", lambda *args: args[1])
    pending = build_runts_pending_payload(proposal_for(final), expected_practice_status="TRA")
    assert pending["pdf_path"] == str(final)
    assert pending["pdf_sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    # No conversion permitted once preparing immutable scope.
    monkeypatch.setattr(pdf, "prepare_runts_upload_pdf", lambda *a: pytest.fail("conversion"))
    from ralfloop_agent.unified_assistant.runts_write import build_runts_reply_approval_scope
    scope = build_runts_reply_approval_scope(SimpleNamespace(domain="runts", action="runts_practice_reply",
        payload=pending, pending_id="synthetic", version=1, payload_digest="b" * 64))
    assert scope["pdf_path"] == str(final)
    assert scope["pdf_sha256"] == pending["pdf_sha256"]


@pytest.mark.parametrize("failure", ["runts_pdfa_validation_failed", "runts_browser_file_unreadable"])
def test_bad_pdf_never_reaches_pending(tmp_path, monkeypatch, failure):
    final = tmp_path / "final.pdf"
    final.write_bytes(b"ordinary-pdf")
    def reject(*a):
        raise pdf.RuntsUploadPdfError(failure)
    monkeypatch.setattr(pdf, "validate_pdfa", reject if "validation" in failure else lambda _: None)
    if "unreadable" in failure:
        monkeypatch.setattr(pdf, "verify_browser_readable", lambda *_: reject())
    with pytest.raises(pdf.RuntsUploadPdfError, match=failure):
        build_runts_pending_payload(proposal_for(final), expected_practice_status="TRA")


def test_ready_requires_real_validation(tmp_path, monkeypatch):
    from test_runts_review_evidence import context, SOURCE
    from ralfloop_agent.unified_assistant.runts_document_prepare import FinalDocumentValidation, prepare_document_review
    from ralfloop_agent.unified_assistant.memory_service import MemoryService
    final = tmp_path / "invalid.pdf"
    final.write_bytes(b"invalid")
    digest = hashlib.sha256(final.read_bytes()).hexdigest()
    document = SOURCE.model_copy(update={"system": "runtsuite", "native_id": "doc", "locator": str(final), "content_hash": digest})
    validation = FinalDocumentValidation(exercise=2025, artifact_sha256=digest,
        semantic_sha256="b" * 64, golden_semantic_sha256="b" * 64, decision_refs=(SOURCE,))
    monkeypatch.setenv("BOTTAZZI_RUNTS_VERAPDF", "/nonexistent/validator")
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        with pytest.raises(pdf.RuntsUploadPdfError):
            prepare_document_review(memory, practice_id="123", message_id=SOURCE.native_id,
                document=document, provenance=(SOURCE,), blockers=(),
                review_context=context().model_copy(update={"pdf_path": str(final)}), final_validation=validation)


@pytest.mark.parametrize("report", [b"<report/>", b"invalid", b'<report><validationReport isCompliant="false"/></report>',
    b'<report><validationReport isCompliant="true"/><validationReport isCompliant="true"/></report>'])
def test_validation_fails_closed(monkeypatch, report):
    monkeypatch.setattr(pdf, "_run", lambda args, code: b"veraPDF 1.30.2\n" if "--version" in args else report)
    with pytest.raises(pdf.RuntsUploadPdfError, match="runts_pdfa_validation_failed"):
        pdf.validate_pdfa("unused")


def test_no_validator_is_failure(monkeypatch):
    monkeypatch.setenv("BOTTAZZI_RUNTS_VERAPDF", "/nonexistent/verapdf")
    with pytest.raises(pdf.RuntsUploadPdfError, match="validator_unavailable"):
        pdf.validate_pdfa("unused")


def test_real_browser_probe_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(probe, "ROOT", tmp_path.resolve())
    path = tmp_path / "final.pdf"
    path.write_bytes(b"synthetic")
    digest = hashlib.sha256(b"synthetic").hexdigest()
    assert probe.probe_runts_upload_file(str(path), digest)["readable"]
    path.chmod(0)
    try:
        assert not probe.probe_runts_upload_file(str(path), digest)["readable"]
    finally:
        path.chmod(0o600)


def test_probe_client_unavailable_fails_closed(monkeypatch):
    monkeypatch.setenv("BOTTAZZI_RUNTS_BROWSER_PROBE_SOCKET", "/nonexistent/probe.sock")
    with pytest.raises(pdf.RuntsUploadPdfError, match="probe_unavailable"):
        pdf.verify_browser_readable("/tmp/unused", "0" * 64)


def test_probe_path_security(tmp_path, monkeypatch):
    root = tmp_path / "root"; root.mkdir()
    outside = tmp_path / "outside.pdf"; outside.write_bytes(b"outside")
    (root / "link.pdf").symlink_to(outside)
    monkeypatch.setattr(probe, "ROOT", root.resolve())
    digest = hashlib.sha256(outside.read_bytes()).hexdigest()
    assert probe.probe_runts_upload_file(str(outside), digest)["error_code"] == "runts_browser_path_denied"
    assert probe.probe_runts_upload_file(str(root / "link.pdf"), digest)["error_code"] == "runts_browser_path_denied"
    assert probe.probe_runts_upload_file(str(root / "../outside.pdf"), digest)["error_code"] == "runts_browser_path_denied"


@pytest.mark.parametrize("phase", ["conversion", "validation", "semantics", "browser"])
def test_finalization_failure_never_publishes(tmp_path, monkeypatch, phase):
    source, final = tmp_path / "source.pdf", tmp_path / "final.pdf"
    source.write_bytes(b"original")
    def run(args, code):
        assert args[0] == "gs"  # fake converter only; no HTTP/POST/browser
        if phase == "conversion":
            raise pdf.RuntsUploadPdfError(code)
        Path(next(a.split("=", 1)[1] for a in args if a.startswith("-sOutputFile="))).write_bytes(b"converted")
        return b""
    monkeypatch.setattr(pdf, "_run", run)
    monkeypatch.setattr(pdf, "_semantics", lambda p: (1, b"changed" if phase == "semantics" and p != source else b"original"))
    def validate(path):
        if phase == "validation":
            raise pdf.RuntsUploadPdfError("runts_pdfa_validation_failed")
    monkeypatch.setattr(pdf, "validate_pdfa", validate)
    def browser(path):
        raise pdf.RuntsUploadPdfError("runts_browser_file_unreadable")
    monkeypatch.setattr(pdf, "ensure_runts_browser_readable", browser)
    with pytest.raises(pdf.RuntsUploadPdfError):
        pdf.prepare_runts_upload_pdf(source, final)
    assert not final.exists()
    assert source.read_bytes() == b"original"


def test_private_prepare_acl_is_minimal(tmp_path, monkeypatch):
    directory = tmp_path / "prepare-private"
    directory.mkdir(mode=0o700)
    path = directory / "final.pdf"
    path.write_bytes(b"synthetic")
    path.chmod(0o600)
    # Simulate a distinct browser UID while inspecting the exact ACL operations.
    monkeypatch.setattr(pdf.pwd, "getpwnam", lambda _: SimpleNamespace(pw_uid=99999))
    commands = []
    def run(args, code):
        commands.append(args)
        return b"user::rwx\ngroup::---\nother::---\n"
    monkeypatch.setattr(pdf, "_run", run)
    monkeypatch.setattr(pdf, "verify_browser_readable", lambda *_: "verified")
    assert pdf.ensure_runts_browser_readable(path) == "verified"
    assert ["setfacl", "-m", "u:bandi:--x", str(directory)] in commands
    assert ["setfacl", "-m", "u:bandi:r--", str(path)] in commands
    assert all("777" not in str(command) for command in commands)


def test_approval_readonly_no_conversion(tmp_path, monkeypatch):
    path = tmp_path / "final.pdf"
    path.write_bytes(b"final-pdfa")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    checked = []
    monkeypatch.setattr(pdf, "validate_pdfa", lambda p: checked.append(p))
    monkeypatch.setattr(pdf, "verify_browser_readable", lambda *args: args[1])
    monkeypatch.setattr(pdf, "prepare_runts_upload_pdf", lambda *a: pytest.fail("post-approval conversion"))
    pdf.verify_runts_upload_pdf(path, digest)
    assert checked == [path]
    with pytest.raises(pdf.RuntsUploadPdfError, match="hash_mismatch"):
        pdf.verify_runts_upload_pdf(path, "0" * 64)


@pytest.mark.parametrize("part", ["1", "2"])
def test_real_conversion_and_validation(tmp_path, monkeypatch, part):
    monkeypatch.setenv("BOTTAZZI_RUNTS_PDFA_PART", part)
    monkeypatch.setattr(pdf, "verify_browser_readable", lambda *args: args[1])
    validator = os.environ.get("BOTTAZZI_RUNTS_VERAPDF", "verapdf")
    if not shutil.which(validator):
        pytest.skip("Independent veraPDF executable required for integration")
    source = tmp_path / "source.pdf"
    ps = tmp_path / "source.ps"
    ps.write_text("%!PS\n/Helvetica findfont 12 scalefont setfont\n72 720 moveto (Income 16366.87 Expense 15654.17) show\nshowpage\n")
    subprocess.run(["gs", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite", f"-sOutputFile={source}", str(ps)], check=True, capture_output=True)
    with pytest.raises(pdf.RuntsUploadPdfError):
        pdf.validate_pdfa(source)
    final = tmp_path / "final.pdf"
    digest = pdf.prepare_runts_upload_pdf(source, final)
    assert digest == hashlib.sha256(final.read_bytes()).hexdigest()
    assert digest != hashlib.sha256(source.read_bytes()).hexdigest()
    assert pdf._semantics(source) == pdf._semantics(final)
    assert final.stat().st_mode & 0o007 == 0
    pdf.verify_runts_upload_pdf(final, digest)
    with pytest.raises(pdf.RuntsUploadPdfError, match="already_exists"):
        pdf.prepare_runts_upload_pdf(source, final)


def test_writer_checks_final_before_network(tmp_path, monkeypatch):
    from ralfloop_agent.unified_assistant.runts_browser_write import RuntsAuthenticatedCdpWriteTransport
    final = tmp_path / "final.pdf"
    final.write_bytes(b"synthetic-final")
    monkeypatch.setattr(pdf, "validate_pdfa", lambda _: None)
    monkeypatch.setattr(pdf, "verify_browser_readable", lambda *args: args[1])
    payload = build_runts_pending_payload(proposal_for(final), expected_practice_status="TRA")
    seen = []
    def verify(path, digest):
        seen.append((path, digest))
        raise pdf.RuntsUploadPdfError("runts_pdfa_validation_failed")
    monkeypatch.setattr(pdf, "verify_runts_upload_pdf", verify)
    writer = RuntsAuthenticatedCdpWriteTransport()
    monkeypatch.setattr(writer, "_page", lambda: pytest.fail("network must not start"))
    with pytest.raises(pdf.RuntsUploadPdfError):
        writer.preflight(payload)
    assert seen == [(final, payload["pdf_sha256"])]
