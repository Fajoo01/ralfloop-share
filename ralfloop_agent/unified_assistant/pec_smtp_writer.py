from __future__ import annotations

import hashlib
import mimetypes
import os
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
import smtplib
import ssl
from typing import Any, Callable, Mapping, Sequence

from ralfloop_agent.domains.domain_approval import effective_approval_status, scope_digest
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore


PEC_SEND_ACTION = "pec_send"
PEC_PROVIDER_ID = "pec.smtp"
MAX_ATTACHMENTS = 20
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 50 * 1024 * 1024


class PecWriterError(RuntimeError):
    pass


@dataclass(frozen=True)
class PecSmtpConfig:
    host: str
    port: int
    username: str
    password: str
    timeout: float
    attachment_roots: tuple[Path, ...]

    @classmethod
    def from_environment(cls) -> "PecSmtpConfig":
        host = os.getenv("BOTTAZZI_PEC_SMTP_HOST", "smtps.pec.aruba.it").strip()
        username = (
            os.getenv("BOTTAZZI_PEC_SMTP_USERNAME", "").strip()
            or os.getenv("BOTTAZZI_PEC_IMAP_USERNAME", "").strip()
        )
        password_file = (
            os.getenv("BOTTAZZI_PEC_SMTP_PASSWORD_FILE", "").strip()
            or os.getenv("BOTTAZZI_PEC_IMAP_PASSWORD_FILE", "").strip()
        )
        password = ""
        if password_file:
            password = Path(password_file).read_text(encoding="utf-8").strip()
        else:
            password = (
                os.getenv("BOTTAZZI_PEC_SMTP_PASSWORD", "").strip()
                or os.getenv("BOTTAZZI_PEC_IMAP_PASSWORD", "").strip()
            )
        try:
            port = int(os.getenv("BOTTAZZI_PEC_SMTP_PORT", "465"))
            timeout = float(os.getenv("BOTTAZZI_PEC_SMTP_TIMEOUT", "20"))
        except ValueError as exc:
            raise PecWriterError("pec_smtp_config_invalid") from exc
        raw_roots = os.getenv(
            "BOTTAZZI_PEC_WRITE_ATTACHMENT_ROOTS",
            "/var/lib/ralfloop/pec-outbox",
        )
        roots = tuple(Path(item).resolve() for item in raw_roots.split(":") if item.strip())
        if not host or not username or not password:
            raise PecWriterError("pec_smtp_auth_missing")
        if not 1 <= port <= 65535 or timeout <= 0 or not roots:
            raise PecWriterError("pec_smtp_config_invalid")
        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            timeout=timeout,
            attachment_roots=roots,
        )


def _normalized_recipient(value: str) -> str:
    value = value.strip()
    if (
        not value
        or len(value) > 320
        or value.count("@") != 1
        or any(ch in value for ch in "\r\n,;<>")
    ):
        raise PecWriterError("pec_recipient_invalid")
    local, domain = value.rsplit("@", 1)
    if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise PecWriterError("pec_recipient_invalid")
    return value


def _normalized_subject(value: str) -> str:
    if "\r" in value or "\n" in value:
        raise PecWriterError("pec_subject_invalid")
    value = " ".join(value.split())
    if not value or len(value) > 500:
        raise PecWriterError("pec_subject_invalid")
    return value


def _normalized_body(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value or len(value) > 100_000:
        raise PecWriterError("pec_body_invalid")
    return value


def _allowed_path(path: Path, roots: tuple[Path, ...]) -> bool:
    resolved = path.resolve(strict=True)
    return any(resolved == root or root in resolved.parents for root in roots)


def _attachment_rows(paths: Sequence[str], roots: tuple[Path, ...]) -> tuple[dict[str, Any], ...]:
    if len(paths) > MAX_ATTACHMENTS:
        raise PecWriterError("pec_too_many_attachments")
    rows: list[dict[str, Any]] = []
    total = 0
    seen: set[Path] = set()
    for raw in paths:
        path = Path(str(raw)).expanduser()
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise PecWriterError("pec_attachment_missing") from exc
        if resolved in seen:
            raise PecWriterError("pec_attachment_duplicate")
        seen.add(resolved)
        if not _allowed_path(resolved, roots) or not resolved.is_file():
            raise PecWriterError("pec_attachment_path_denied")
        size = resolved.stat().st_size
        if size > MAX_ATTACHMENT_BYTES:
            raise PecWriterError("pec_attachment_too_large")
        total += size
        if total > MAX_TOTAL_ATTACHMENT_BYTES:
            raise PecWriterError("pec_attachments_too_large")
        rows.append(
            {
                "path": str(resolved),
                "name": resolved.name,
                "bytes": size,
                "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(),
            }
        )
    return tuple(rows)


def build_pec_send_scope(
    *,
    account: str,
    recipient: str,
    subject: str,
    body: str,
    attachment_paths: Sequence[str] = (),
    attachment_roots: tuple[Path, ...],
) -> dict[str, Any]:
    recipient = _normalized_recipient(recipient)
    subject = _normalized_subject(subject)
    body = _normalized_body(body)
    attachments = _attachment_rows(attachment_paths, attachment_roots)
    core = {
        "action": PEC_SEND_ACTION,
        "version": 1,
        "account": account.strip().casefold(),
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "attachments": list(attachments),
    }
    digest = scope_digest(core)
    return {
        **core,
        "artifact_sha256": digest,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "idempotency_key": "pec:" + hashlib.sha256(
            (account.strip().casefold() + "\0" + digest).encode("utf-8")
        ).hexdigest(),
    }


class PecSmtpWriter:
    """Approval-bound PEC sender. No read operations are exposed here."""

    def __init__(
        self,
        config: PecSmtpConfig,
        *,
        store: DomainApprovalStore,
        smtp_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.smtp_factory = smtp_factory

    def _connect(self):
        context = ssl.create_default_context()
        factory = self.smtp_factory or smtplib.SMTP_SSL
        client = factory(
            self.config.host,
            self.config.port,
            timeout=self.config.timeout,
            context=context,
        )
        client.ehlo()
        client.login(self.config.username, self.config.password)
        return client

    def preflight(self) -> dict[str, Any]:
        client = None
        try:
            client = self._connect()
            code, _ = client.noop()
            if int(code) != 250:
                raise PecWriterError("pec_smtp_noop_failed")
            return {
                "ok": True,
                "status": "ready",
                "provider": "aruba_pec_smtp",
                "account": self.config.username,
                "host": self.config.host,
                "port": self.config.port,
                "writes": 0,
                "sends": 0,
            }
        except PecWriterError:
            raise
        except Exception as exc:
            raise PecWriterError("pec_smtp_preflight_failed") from exc
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass

    def prepare_send(
        self,
        *,
        recipient: str,
        subject: str,
        body: str,
        attachment_paths: Sequence[str] = (),
        requested_by: str = "bot-tazzi",
    ) -> dict[str, Any]:
        scope = build_pec_send_scope(
            account=self.config.username,
            recipient=recipient,
            subject=subject,
            body=body,
            attachment_paths=attachment_paths,
            attachment_roots=self.config.attachment_roots,
        )
        result = self.store.create_request(
            action=PEC_SEND_ACTION,
            bando_id=PEC_PROVIDER_ID,
            version="1",
            scope=scope,
            requested_by=requested_by,
        )
        request = result.get("request") if isinstance(result, Mapping) else None
        if not isinstance(request, Mapping):
            return {
                "ok": False,
                "status": str(result.get("status") or "approval_request_failed"),
                "writes": 0,
                "sends": 0,
            }
        return {
            "ok": True,
            "status": "approval_required",
            "approval_request_id": str(request["request_id"]),
            "scope_digest_short": str(request["scope_digest_short"]),
            "recipient": scope["recipient"],
            "subject": scope["subject"],
            "attachments": [
                {"name": row["name"], "bytes": row["bytes"], "sha256": row["sha256"]}
                for row in scope["attachments"]
            ],
            "writes": 0,
            "sends": 0,
            "approval_writes": 1,
        }

    def send_approved(
        self,
        *,
        approval_request_id: str,
        recipient: str,
        subject: str,
        body: str,
        attachment_paths: Sequence[str] = (),
    ) -> dict[str, Any]:
        scope = build_pec_send_scope(
            account=self.config.username,
            recipient=recipient,
            subject=subject,
            body=body,
            attachment_paths=attachment_paths,
            attachment_roots=self.config.attachment_roots,
        )
        row = self.store.get_request(approval_request_id)
        if row is None:
            return self._blocked("approval_not_found")
        if row.get("action") != PEC_SEND_ACTION or row.get("bando_id") != PEC_PROVIDER_ID:
            return self._blocked("approval_scope_wrong_provider")
        if str(row.get("scope_digest") or "") != scope_digest(scope):
            if str(row.get("status") or "") in {"pending", "approved"}:
                self.store.mark_stale(approval_request_id, ["pec_artifact_changed"])
            return self._blocked("approval_scope_mismatch")
        status = effective_approval_status(row)
        if status != "approved":
            return self._blocked(
                "approval_expired" if status == "expired" else f"approval_{status}"
            )

        self.preflight()
        claim = self.store.claim_execution(approval_request_id, action=PEC_SEND_ACTION)
        if not claim.get("claimed"):
            current = str(claim.get("status") or "approval_claim_failed")
            if current == "already_consumed":
                return {
                    "ok": True,
                    "status": "already_executed",
                    "writes": 0,
                    "sends": 0,
                    "retry_allowed": False,
                }
            return self._blocked(current)

        message = EmailMessage()
        message["From"] = self.config.username
        message["To"] = scope["recipient"]
        message["Subject"] = scope["subject"]
        message.set_content(scope["body"])

        for item in scope["attachments"]:
            path = Path(str(item["path"]))
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                result = {
                    "status": "DRAFT_CHANGED",
                    "sent": False,
                    "retry_allowed": False,
                }
                self.store.finish_claimed_execution(
                    approval_request_id,
                    action=PEC_SEND_ACTION,
                    success=False,
                    result=result,
                )
                return {**result, "ok": False, "writes": 0, "sends": 0}
            mime, _ = mimetypes.guess_type(path.name)
            maintype, subtype = (mime or "application/octet-stream").split("/", 1)
            message.add_attachment(
                path.read_bytes(),
                maintype=maintype,
                subtype=subtype,
                filename=path.name,
            )

        client = None
        try:
            client = self._connect()
            refused = client.send_message(
                message,
                from_addr=self.config.username,
                to_addrs=[scope["recipient"]],
            )
            if refused:
                raise PecWriterError("pec_recipient_refused")
            result = {
                "status": "submitted_to_pec_provider",
                "sent": True,
                "delivery_verified": False,
                "receipt_verification": "pending_reader_receipts",
                "retry_allowed": False,
                "recipient": scope["recipient"],
                "subject": scope["subject"],
            }
            self.store.finish_claimed_execution(
                approval_request_id,
                action=PEC_SEND_ACTION,
                success=True,
                result=result,
            )
            return {**result, "ok": True, "writes": 1, "sends": 1}
        except Exception:
            result = {
                "status": "EXECUTION_UNCERTAIN",
                "sent": False,
                "delivery_verified": False,
                "retry_allowed": False,
            }
            self.store.finish_claimed_execution(
                approval_request_id,
                action=PEC_SEND_ACTION,
                success=False,
                result=result,
            )
            return {**result, "ok": False, "writes": 0, "sends": 0}
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass

    @staticmethod
    def _blocked(status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "status": status,
            "writes": 0,
            "sends": 0,
            "retry_allowed": False,
        }


__all__ = [
    "PEC_PROVIDER_ID",
    "PEC_SEND_ACTION",
    "PecSmtpConfig",
    "PecSmtpWriter",
    "PecWriterError",
    "build_pec_send_scope",
]
