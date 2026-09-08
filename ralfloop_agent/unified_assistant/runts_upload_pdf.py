"""Fail-closed, local-only RUNTS PDF finalization. Never used to convert after approval."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import socket
import subprocess
import tempfile
import xml.etree.ElementTree as ET


class RuntsUploadPdfError(ValueError):
    pass


VERAPDF_VERSION = "1.30.2"
PROBE_SOCKET = "/run/ralf-runts-browser-probe.sock"


def _pdfa_part():
    # WeasyPrint's transparency groups cause GS PDF/A-1 conversion to rasterize
    # the real two-page form (all extractable text lost). PDF/A-2 retains it.
    part = os.environ.get("BOTTAZZI_RUNTS_PDFA_PART", "2")
    if part not in {"1", "2"}:
        raise RuntsUploadPdfError("runts_pdfa_profile_invalid")
    return part


def _run(args, code):
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=120).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntsUploadPdfError(code) from exc


def verify_verapdf_runtime():
    executable = os.environ.get("BOTTAZZI_RUNTS_VERAPDF", "verapdf")
    output = _run([executable, "--version"], "runts_pdfa_validator_unavailable").decode(errors="replace")
    if not output.startswith("veraPDF " + VERAPDF_VERSION + "\n"):
        raise RuntsUploadPdfError("runts_pdfa_validator_version_incompatible")


def validate_pdfa(path):
    """Independent ISO validation; XMP assertions alone never qualify."""
    verify_verapdf_runtime()
    report = _run([os.environ.get("BOTTAZZI_RUNTS_VERAPDF", "verapdf"),
                   "--format", "xml", "--flavour", _pdfa_part() + "b", str(path)],
                  "runts_pdfa_validation_failed")
    try:
        root = ET.fromstring(report)
        validations = [n for n in root.iter() if n.tag.split("}")[-1] == "validationReport"]
        if len(validations) != 1 or validations[0].get("isCompliant") != "true":
            raise ValueError("not compliant")
    except (ET.ParseError, ValueError) as exc:
        raise RuntsUploadPdfError("runts_pdfa_validation_failed") from exc


def verify_browser_readable(path, expected_sha256=None):
    """Call the fixed-purpose `bandi` Unix socket; no subprocess escalation."""
    path = Path(path).absolute()
    expected_sha256 = expected_sha256 or hashlib.sha256(path.read_bytes()).hexdigest()
    request = json.dumps({"path": str(path), "expected_sha256": expected_sha256}, separators=(",", ":")).encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(os.environ.get("BOTTAZZI_RUNTS_BROWSER_PROBE_SOCKET", PROBE_SOCKET))
            connection.sendall(request)
            response = json.loads(connection.recv(4096).decode())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntsUploadPdfError("runts_browser_probe_unavailable") from exc
    code = str(response.get("error_code") or "")
    if not response.get("readable"):
        raise RuntsUploadPdfError(code if code in {"runts_browser_file_unreadable", "runts_browser_file_hash_mismatch", "runts_browser_path_denied"} else "runts_browser_probe_unavailable")
    if response.get("sha256") != expected_sha256:
        raise RuntsUploadPdfError("runts_browser_file_hash_mismatch")
    return expected_sha256


def ensure_runts_browser_readable(path):
    path = Path(path).absolute()
    # Grant named-user rights only on owned directories. Never chmod/chown or
    # widen unrelated parents; inaccessible unowned parents fail the real probe.
    uid = os.geteuid()
    browser_uid = pwd.getpwnam("bandi").pw_uid
    for parent in reversed(path.parents):
        stat = parent.stat()
        if (parent == Path("/") or stat.st_uid != uid
                or stat.st_uid == browser_uid or stat.st_mode & 0o001):
            continue
        acl = _run(["getfacl", "-cp", str(parent)], "runts_browser_path_untraversable").decode()
        _assert_acl_has_no_other_named_users(acl, os.getuid())
        # Set an explicit minimum mask. `-n` prevents setfacl from widening an
        # inherited mask and thus unrelated named entries.
        _run(["setfacl", "-n", "-m", "m::--x,u:bandi:--x", str(parent)],
             "runts_browser_path_untraversable")
    file_acl = _run(["getfacl", "-cp", str(path)], "runts_browser_file_unreadable").decode()
    _assert_acl_has_no_other_named_users(file_acl, os.getuid())
    _run(["setfacl", "-n", "-m", "m::r--,u:bandi:r--", str(path)], "runts_browser_file_unreadable")
    return verify_browser_readable(path, hashlib.sha256(path.read_bytes()).hexdigest())


def _assert_acl_has_no_other_named_users(acl: str, owner_uid: int) -> None:
    """Mask changes are safe only for the owner and the one browser grant."""
    owner = pwd.getpwuid(owner_uid).pw_name
    for line in acl.splitlines():
        if line.startswith(("user:", "default:user:")):
            parts = line.split(":")
            name = parts[1] if len(parts) > 2 else ""
            if name and name not in {"bandi", owner}:
                raise RuntsUploadPdfError("runts_browser_path_untraversable")


def verify_runts_upload_pdf(path, expected_sha256):
    """Read-only approval boundary: no conversion, ACL mutation or repair."""
    before = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if before != expected_sha256:
        raise RuntsUploadPdfError("runts_pdf_hash_mismatch")
    validate_pdfa(path)
    if verify_browser_readable(path, before) != before:
        raise RuntsUploadPdfError("runts_pdf_hash_mismatch")


def _semantics(path):
    info = _run(["pdfinfo", str(path)], "runts_pdf_integrity_failed").decode()
    pages = re.search(r"^Pages:\s+(\d+)$", info, re.M)
    text = _run(["pdftotext", "-layout", str(path), "-"], "runts_pdf_integrity_failed")
    if not pages or not Path(path).stat().st_size:
        raise RuntsUploadPdfError("runts_pdf_integrity_failed")
    return int(pages[1]), b" ".join(text.split())


def prepare_runts_upload_pdf(source, final):
    """Finalize once, before hashing/proposal. Refuse to replace existing output."""
    source, final = Path(source).resolve(), Path(final).absolute()
    if final.exists() or source == final:
        raise RuntsUploadPdfError("runts_pdf_final_already_exists")
    icc = Path(os.environ.get("BOTTAZZI_RUNTS_PDFA_ICC", "/usr/share/color/icc/ghostscript/srgb.icc")).resolve()
    if not icc.is_file():
        raise RuntsUploadPdfError("runts_pdfa_icc_unavailable")
    original = _semantics(source)
    with tempfile.TemporaryDirectory(prefix=".pdfa-", dir=final.parent) as work:
        output = Path(work) / "final.pdf"
        definition = Path(work) / "output-intent.ps"
        escaped = str(icc).replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        definition.write_text("%!PS\n[/_objdef {icc_PDFA} /type /stream /OBJ pdfmark\n"
            "[{icc_PDFA} << /N 3 >> /PUT pdfmark\n"
            f"[{{icc_PDFA}} ({escaped}) (r) file /PUT pdfmark\n"
            "[/_objdef {OutputIntent_PDFA} /type /dict /OBJ pdfmark\n"
            "[{OutputIntent_PDFA} << /Type /OutputIntent /S /GTS_PDFA1 "
            "/DestOutputProfile {icc_PDFA} /OutputConditionIdentifier (sRGB) >> /PUT pdfmark\n"
            "[{Catalog} << /OutputIntents [ {OutputIntent_PDFA} ] >> /PUT pdfmark\n")
        _run(["gs", "-dBATCH", "-dNOPAUSE", "-dSAFER", "-sDEVICE=pdfwrite",
              "-dPDFA=" + _pdfa_part(), "-dPDFACompatibilityPolicy=2", "-sColorConversionStrategy=RGB",
              "-dEmbedAllFonts=true", f"--permit-file-read={icc}",
              f"-sOutputFile={output}", str(definition), str(source)], "runts_pdfa_conversion_failed")
        validate_pdfa(output)
        if _semantics(output) != original:
            raise RuntsUploadPdfError("runts_pdf_semantic_mismatch")
        # Exclusive creation: never overwrite a previously approved document.
        with os.fdopen(os.open(final, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as target:
            target.write(output.read_bytes())
        try:
            digest = ensure_runts_browser_readable(final)
            verify_runts_upload_pdf(final, digest)
        except Exception:
            final.unlink()  # only the newly created, unapproved artifact
            raise
    return digest
