from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Any

from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore


DOCUMENT_SIGN_ACTION = "document_sign"
DEFAULT_ARUBASIGN = Path(
    "/home/bandi/Scaricati/ArubaSign-latest-LINUX/app/lin-x64/ArubaSign"
)
DEFAULT_OPENSSL = Path("/usr/bin/openssl")
DEFAULT_ALLOWED_ROOTS = (Path("/var/lib/ralfloop/pec-outbox"),)


class DigitalSigningError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve()
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in roots)


def _allowed_roots_from_env() -> tuple[Path, ...]:
    raw = os.getenv("BOTTAZZI_SIGN_ALLOWED_ROOTS", "").strip()
    if not raw:
        return DEFAULT_ALLOWED_ROOTS
    rows = tuple(Path(item).expanduser().resolve() for item in raw.split(":") if item.strip())
    return rows or DEFAULT_ALLOWED_ROOTS


class ArubaSignApprovalWorkflow:
    """Approval-bound human-in-the-loop digital signature workflow.

    Bot-tazzi never receives or stores PIN/password/OTP. The approval only binds an
    exact source document hash. ArubaSign remains responsible for credential entry
    and signing; this workflow independently verifies the produced CMS/P7M artifact.
    """

    def __init__(
        self,
        store: DomainApprovalStore,
        *,
        arubasign_path: str | Path = DEFAULT_ARUBASIGN,
        openssl_path: str | Path = DEFAULT_OPENSSL,
        allowed_roots: tuple[Path, ...] | None = None,
        expected_signer: str = "Fabio Fagioli",
    ) -> None:
        self.store = store
        self.arubasign_path = Path(arubasign_path)
        self.openssl_path = Path(openssl_path)
        self.allowed_roots = allowed_roots or _allowed_roots_from_env()
        self.expected_signer = expected_signer

    @classmethod
    def from_environment(cls, store: DomainApprovalStore) -> "ArubaSignApprovalWorkflow":
        return cls(
            store,
            arubasign_path=os.getenv("BOTTAZZI_ARUBASIGN_PATH", str(DEFAULT_ARUBASIGN)),
            openssl_path=os.getenv("BOTTAZZI_OPENSSL_PATH", str(DEFAULT_OPENSSL)),
            expected_signer=os.getenv("BOTTAZZI_SIGN_EXPECTED_SIGNER", "Fabio Fagioli"),
        )

    def preflight(self) -> dict[str, Any]:
        display = os.getenv("BOTTAZZI_ARUBASIGN_DISPLAY", os.getenv("DISPLAY", "")).strip()
        return {
            "ok": self.arubasign_path.is_file() and os.access(self.arubasign_path, os.X_OK)
            and self.openssl_path.is_file() and os.access(self.openssl_path, os.X_OK),
            "arubasign_available": self.arubasign_path.is_file() and os.access(self.arubasign_path, os.X_OK),
            "openssl_available": self.openssl_path.is_file() and os.access(self.openssl_path, os.X_OK),
            "gui_available": bool(display),
            "display": display or None,
            "pin_otp_required_user": True,
            "secrets_managed_by_bottazzi": False,
            "writes": 0,
            "sends": 0,
        }

    def prepare(self, source_path: str | Path, *, requested_by: str = "bot-tazzi") -> dict[str, Any]:
        raw_source = Path(source_path).expanduser()
        if raw_source.is_symlink():
            raise DigitalSigningError("document_source_symlink_denied")
        source = raw_source.resolve()
        if not source.is_file():
            raise DigitalSigningError("document_source_invalid")
        if not _is_within(source, self.allowed_roots):
            raise DigitalSigningError("document_source_outside_allowed_roots")
        preflight = self.preflight()
        if not preflight["ok"]:
            raise DigitalSigningError("digital_signing_runtime_unavailable")
        source_hash = _sha256(source)
        output = source.with_name(source.name + ".p7m")
        scope = {
            "action": DOCUMENT_SIGN_ACTION,
            "source_path": str(source),
            "source_sha256": source_hash,
            "source_bytes": source.stat().st_size,
            "output_path": str(output),
            "expected_signer": self.expected_signer,
            "signature_format": "CMS_CAdES_P7M",
            "provider": "ArubaSign",
            "pin_otp_policy": "user_entry_only_never_stored",
            "send_authorized": False,
        }
        created = self.store.create_request(
            action=DOCUMENT_SIGN_ACTION,
            bando_id="document-sign",
            version="1",
            scope=scope,
            requested_by=requested_by,
        )
        if created.get("status") != "pending":
            return {"ok": False, **created, "writes": 0, "sends": 0}
        request = created["request"]
        return {
            "ok": True,
            "status": "approval_required",
            "approval_request_id": request["request_id"],
            "scope_digest_short": request["scope_digest_short"],
            "source_path": str(source),
            "source_sha256": source_hash,
            "output_path": str(output),
            "expected_signer": self.expected_signer,
            "gui_available": preflight["gui_available"],
            "pin_otp_required_user": True,
            "secrets_managed_by_bottazzi": False,
            "writes": 0,
            "sends": 0,
        }

    def handoff_approved(self, approval_request_id: str) -> dict[str, Any]:
        request = self.store.get_request(approval_request_id)
        if not request or request.get("action") != DOCUMENT_SIGN_ACTION:
            raise DigitalSigningError("sign_approval_not_found")
        if request.get("status") != "approved":
            return {"ok": False, "status": f"not_approved:{request.get('status')}", "launched": False}
        scope = request.get("scope") or {}
        source = Path(str(scope.get("source_path") or "")).resolve()
        if not source.is_file() or not _is_within(source, self.allowed_roots):
            self.store.mark_stale(approval_request_id, ["source_missing_or_outside_allowed_roots"])
            return {"ok": False, "status": "stale", "launched": False}
        if _sha256(source) != str(scope.get("source_sha256") or ""):
            self.store.mark_stale(approval_request_id, ["source_hash_changed"])
            return {"ok": False, "status": "stale", "launched": False}
        preflight = self.preflight()
        if not preflight["gui_available"]:
            return {
                "ok": True,
                "status": "user_interaction_required",
                "launched": False,
                "source_path": str(source),
                "approval_request_id": approval_request_id,
                "pin_otp_required_user": True,
                "secrets_managed_by_bottazzi": False,
            }
        env = {
            "PATH": os.getenv("PATH", "/usr/bin:/bin"),
            "HOME": os.getenv("BOTTAZZI_ARUBASIGN_HOME", "/home/bandi"),
            "DISPLAY": str(preflight["display"]),
        }
        for name in ("XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
            value = os.getenv("BOTTAZZI_ARUBASIGN_" + name, os.getenv(name, "")).strip()
            if value:
                env[name] = value
        subprocess.Popen(
            [str(self.arubasign_path), "--no-sandbox", str(source)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            env=env,
            start_new_session=True,
        )
        return {
            "ok": True,
            "status": "user_interaction_required",
            "launched": True,
            "source_path": str(source),
            "expected_output_path": str(scope.get("output_path") or ""),
            "approval_request_id": approval_request_id,
            "pin_otp_required_user": True,
            "secrets_managed_by_bottazzi": False,
        }

    def verify_approved_result(self, approval_request_id: str, signed_path: str | Path | None = None) -> dict[str, Any]:
        request = self.store.get_request(approval_request_id)
        if not request or request.get("action") != DOCUMENT_SIGN_ACTION:
            raise DigitalSigningError("sign_approval_not_found")
        if request.get("status") != "approved":
            return {"ok": False, "status": f"not_approved:{request.get('status')}", "verified": False}
        scope = request.get("scope") or {}
        source = Path(str(scope.get("source_path") or "")).resolve()
        signed = Path(signed_path or str(scope.get("output_path") or "")).expanduser().resolve()
        if not source.is_file() or not signed.is_file() or signed.suffix.casefold() != ".p7m":
            return {"ok": True, "status": "signed_artifact_not_found", "verified": False, "writes": 0, "sends": 0}
        if not _is_within(source, self.allowed_roots) or not _is_within(signed, self.allowed_roots):
            raise DigitalSigningError("signed_artifact_outside_allowed_roots")
        if _sha256(source) != str(scope.get("source_sha256") or ""):
            self.store.mark_stale(approval_request_id, ["source_hash_changed"])
            return {"ok": False, "status": "stale", "verified": False}

        with tempfile.TemporaryDirectory(prefix="bottazzi-sign-verify-") as raw:
            extracted = Path(raw) / "content.bin"
            verify = subprocess.run(
                [str(self.openssl_path), "cms", "-verify", "-inform", "DER", "-in", str(signed), "-noverify", "-out", str(extracted)],
                check=False, text=True, capture_output=True, shell=False, timeout=30,
            )
            if verify.returncode != 0 or not extracted.is_file():
                return {"ok": False, "status": "signature_invalid", "verified": False, "writes": 0, "sends": 0}
            content_hash = _sha256(extracted)
            content_match = content_hash == str(scope.get("source_sha256") or "")
            certs = subprocess.run(
                [str(self.openssl_path), "pkcs7", "-inform", "DER", "-in", str(signed), "-print_certs", "-noout"],
                check=False, text=True, capture_output=True, shell=False, timeout=30,
            )
        subject = next((line.strip()[8:] for line in certs.stdout.splitlines() if line.startswith("subject=")), "")
        issuer = next((line.strip()[7:] for line in certs.stdout.splitlines() if line.startswith("issuer=")), "")
        expected_tokens = [token.casefold() for token in re.findall(r"[A-Za-zÀ-ÿ]+", str(scope.get("expected_signer") or self.expected_signer))]
        folded_subject = subject.casefold()
        signer_match = bool(expected_tokens) and all(token in folded_subject for token in expected_tokens)
        verified = verify.returncode == 0 and content_match and certs.returncode == 0 and signer_match
        result = {
            "ok": verified,
            "status": "verified" if verified else "signature_identity_or_content_mismatch",
            "verified": verified,
            "cryptographic_integrity": verify.returncode == 0,
            "content_hash_match": content_match,
            "source_sha256": str(scope.get("source_sha256") or ""),
            "signed_sha256": _sha256(signed),
            "signed_path": str(signed),
            "signer_subject": subject,
            "issuer": issuer,
            "expected_signer_match": signer_match,
            "trust_chain_revalidated_locally": False,
            "pin_otp_observed_by_bottazzi": False,
            "writes": 0,
            "sends": 0,
        }
        if verified:
            self.store.consume(approval_request_id, {"action": DOCUMENT_SIGN_ACTION, **result})
        return result


__all__ = [
    "ArubaSignApprovalWorkflow",
    "DigitalSigningError",
    "DOCUMENT_SIGN_ACTION",
]
