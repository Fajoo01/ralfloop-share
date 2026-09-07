from __future__ import annotations

import hashlib
import imaplib
import os
import re
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

from .pec_runts import PecAttachment, PecMessage
from .platform import SourceRef


class PecImapError(RuntimeError):
    @property
    def status(self) -> str:
        code = str(self)
        if "auth" in code:
            return "AUTH_REQUIRED"
        if any(part in code for part in ("malformed", "invalid")):
            return "MALFORMED_RESPONSE"
        if "incomplete" in code:
            return "INCOMPLETE_SOURCE"
        if "not_found" in code:
            return "NOT_FOUND"
        return "SOURCE_UNAVAILABLE"


@dataclass(frozen=True)
class PecImapConfig:
    host: str
    username: str
    password: str
    port: int = 993
    mailbox: str = "INBOX"
    timeout: float = 20.0
    max_scan: int = 5000

    @classmethod
    def from_environment(cls) -> "PecImapConfig":
        host = os.getenv("BOTTAZZI_PEC_IMAP_HOST", "").strip()
        username = os.getenv("BOTTAZZI_PEC_IMAP_USERNAME", "").strip()

        password_file = os.getenv(
            "BOTTAZZI_PEC_IMAP_PASSWORD_FILE", ""
        ).strip()

        password = ""
        if password_file:
            password = Path(password_file).read_text().strip()
        else:
            password = os.getenv(
                "BOTTAZZI_PEC_IMAP_PASSWORD", ""
            ).strip()

        if not host:
            raise PecImapError("pec_imap_host_missing")
        if not username:
            raise PecImapError("pec_imap_username_missing")
        if not password:
            raise PecImapError("pec_imap_auth_missing")

        try:
            port = int(os.getenv("BOTTAZZI_PEC_IMAP_PORT", "993"))
            timeout = float(
                os.getenv("BOTTAZZI_PEC_IMAP_TIMEOUT", "20")
            )
            max_scan = int(
                os.getenv("BOTTAZZI_PEC_IMAP_MAX_SCAN", "5000")
            )
        except ValueError as exc:
            raise PecImapError("pec_imap_config_invalid") from exc

        if not 1 <= port <= 65535:
            raise PecImapError("pec_imap_port_invalid")
        if not 1 <= max_scan <= 100000:
            raise PecImapError("pec_imap_max_scan_invalid")

        return cls(
            host=host,
            username=username,
            password=password,
            port=port,
            mailbox=os.getenv(
                "BOTTAZZI_PEC_IMAP_MAILBOX", "INBOX"
            ).strip() or "INBOX",
            timeout=timeout,
            max_scan=max_scan,
        )


class PecImapAdapter:
    """
    Read-only PEC provider over IMAPS.

    No STORE, DELETE, MOVE, COPY or SMTP operation is exposed.
    SELECT is always readonly=True and message bodies use BODY.PEEK[].
    """

    def __init__(
        self,
        config: PecImapConfig,
        *,
        client_factory: Callable[..., object] | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self._cache: dict[str, PecMessage] = {}
        self._attachment_cache: dict[tuple[str, str], bytes] = {}

    def list_messages(self, *, limit: int) -> tuple[PecMessage, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("pec_limit_invalid")

        with self._session() as client:
            uids = self._search_uids(client)

            # Most recent messages first.
            selected = tuple(reversed(uids))[:limit]
            return tuple(
                self._fetch_message(client, uid)
                for uid in selected
            )

    def get_message(self, native_id: str) -> PecMessage:
        if native_id in self._cache:
            return self._cache[native_id]

        uid = self._uid_from_native_id(native_id)

        with self._session() as client:
            message = self._fetch_message(client, uid)

        if message.native_id != native_id:
            raise PecImapError("pec_imap_identity_mismatch")

        return message

    def find_by_runts_reference(
        self,
        reference: str,
        *,
        limit: int,
    ) -> tuple[PecMessage, ...]:
        if (
            not re.fullmatch(
                r"[A-Za-z0-9_.:@/-]{1,240}", reference
            )
            or not 1 <= limit <= 100
        ):
            raise ValueError("pec_reference_invalid")

        pattern = re.compile(
            r"(?<![A-Za-z0-9])"
            + re.escape(reference)
            + r"(?![A-Za-z0-9])"
        )

        matches: list[PecMessage] = []

        with self._session() as client:
            uids = self._search_uids(client)

            if len(uids) > self.config.max_scan:
                raise PecImapError("pec_imap_incomplete_source")

            for uid in reversed(uids):
                row = self._fetch_message(client, uid)
                text = row.subject + "\n" + row.body

                if (
                    "runts" in text.casefold()
                    and pattern.search(text)
                ):
                    matches.append(row)

                    if len(matches) > limit:
                        raise PecImapError(
                            "pec_imap_incomplete_source"
                        )

        return tuple(matches)

    def download_attachment(
        self,
        message_id: str,
        attachment_id: str,
    ) -> bytes:
        key = (message_id, attachment_id)

        if key not in self._attachment_cache:
            self.get_message(message_id)

        try:
            return self._attachment_cache[key]
        except KeyError as exc:
            raise PecImapError(
                "pec_imap_attachment_not_found"
            ) from exc

    class _Session:
        def __init__(self, owner: "PecImapAdapter") -> None:
            self.owner = owner
            self.client = None

        def __enter__(self):
            config = self.owner.config

            try:
                if self.owner.client_factory is not None:
                    self.client = self.owner.client_factory(
                        config.host,
                        config.port,
                        timeout=config.timeout,
                    )
                else:
                    context = ssl.create_default_context()
                    self.client = imaplib.IMAP4_SSL(
                        config.host,
                        config.port,
                        ssl_context=context,
                        timeout=config.timeout,
                    )

                typ, _ = self.client.login(
                    config.username,
                    config.password,
                )
                if typ != "OK":
                    raise PecImapError("pec_imap_auth_failed")

                typ, _ = self.client.select(
                    config.mailbox,
                    readonly=True,
                )
                if typ != "OK":
                    raise PecImapError(
                        "pec_imap_mailbox_unavailable"
                    )

                return self.client

            except imaplib.IMAP4.error as exc:
                raise PecImapError(
                    "pec_imap_auth_failed"
                ) from exc
            except PecImapError:
                raise
            except Exception as exc:
                raise PecImapError(
                    "pec_imap_source_unavailable"
                ) from exc

        def __exit__(self, exc_type, exc, tb):
            if self.client is not None:
                try:
                    self.client.close()
                except Exception:
                    pass
                try:
                    self.client.logout()
                except Exception:
                    pass

    def _session(self):
        return self._Session(self)

    def _search_uids(self, client) -> tuple[str, ...]:
        try:
            typ, data = client.uid("SEARCH", None, "ALL")
        except Exception as exc:
            raise PecImapError(
                "pec_imap_search_unavailable"
            ) from exc

        if typ != "OK" or not data:
            raise PecImapError(
                "pec_imap_search_unavailable"
            )

        raw = data[0] or b""

        try:
            values = tuple(
                part.decode("ascii")
                for part in raw.split()
            )
        except Exception as exc:
            raise PecImapError(
                "pec_imap_search_malformed"
            ) from exc

        if any(not uid.isdecimal() for uid in values):
            raise PecImapError(
                "pec_imap_uid_invalid"
            )

        return values

    def _fetch_message(self, client, uid: str) -> PecMessage:
        try:
            typ, data = client.uid(
                "FETCH",
                uid,
                "(FLAGS BODY.PEEK[])",
            )
        except Exception as exc:
            raise PecImapError(
                "pec_imap_fetch_unavailable"
            ) from exc

        if typ != "OK":
            raise PecImapError(
                "pec_imap_fetch_unavailable"
            )

        header = b""
        raw = None

        for item in data or ():
            if (
                isinstance(item, tuple)
                and len(item) >= 2
                and isinstance(item[1], bytes)
            ):
                header = (
                    item[0]
                    if isinstance(item[0], bytes)
                    else b""
                )
                raw = item[1]
                break

        if raw is None:
            raise PecImapError(
                "pec_imap_message_malformed"
            )

        native_id = self._native_id(uid)

        try:
            parsed = BytesParser(
                policy=policy.default
            ).parsebytes(raw)
        except Exception as exc:
            raise PecImapError(
                "pec_imap_message_malformed"
            ) from exc

        subject = _clean_header(
            parsed.get("Subject"), "(no subject)", 500
        )
        sender = _clean_header(
            parsed.get("From"), "unknown@invalid", 320
        )
        received = _received_at(parsed)
        observed = datetime.now(timezone.utc)
        body = _extract_body(parsed)[:100_000]

        attachments = tuple(
            self._extract_attachments(
                parsed,
                native_id=native_id,
            )
        )

        unread = b"\\Seen" not in header
        certified = _certified(parsed, subject)

        source_hash = hashlib.sha256(raw).hexdigest()

        source = SourceRef(
            system="pec",
            native_id=native_id,
            locator=(
                "imaps://"
                + self.config.host
                + "/"
                + self.config.mailbox
                + "?uid="
                + uid
            ),
            observed_at=observed.isoformat(),
            content_hash=source_hash,
        )

        reference = _runts_reference(
            subject + "\n" + body
        )

        message = PecMessage.build(
            native_id=native_id,
            subject=subject,
            sender=sender,
            received_at=received,
            observed_at=observed,
            body=body,
            unread=unread,
            certified=certified,
            attachments=attachments,
            runts_reference=reference,
            source=source,
        )

        self._cache[native_id] = message
        return message

    def _extract_attachments(
        self,
        message: Message,
        *,
        native_id: str,
    ):
        index = 0

        for part in message.walk():
            if part.is_multipart():
                continue

            filename = part.get_filename()
            disposition = (
                part.get_content_disposition() or ""
            ).casefold()

            if not filename and disposition != "attachment":
                continue

            index += 1
            payload = part.get_payload(decode=True) or b""
            digest = hashlib.sha256(payload).hexdigest()

            attachment_id = f"part-{index}-{digest[:16]}"

            self._attachment_cache[
                (native_id, attachment_id)
            ] = payload

            yield PecAttachment(
                attachment_id=attachment_id,
                filename=_clean_header(
                    filename,
                    f"attachment-{index}",
                    500,
                ),
                content_type=(
                    part.get_content_type() or None
                ),
                size=len(payload),
                content_hash=digest,
            )

    def _native_id(self, uid: str) -> str:
        mailbox_hash = hashlib.sha256(
            (
                self.config.host.casefold()
                + "\0"
                + self.config.username.casefold()
                + "\0"
                + self.config.mailbox
            ).encode()
        ).hexdigest()[:12]

        return f"imap.{mailbox_hash}.{uid}"

    @staticmethod
    def _uid_from_native_id(native_id: str) -> str:
        match = re.fullmatch(
            r"imap\.[0-9a-f]{12}\.([0-9]+)",
            native_id,
        )
        if not match:
            raise PecImapError(
                "pec_imap_native_id_invalid"
            )
        return match.group(1)


def _clean_header(
    value,
    fallback: str,
    maximum: int,
) -> str:
    text = " ".join(str(value or fallback).split())
    return (text or fallback)[:maximum]


def _received_at(message: Message) -> datetime:
    value = message.get("Date")

    if value:
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            pass

    return datetime.now(timezone.utc)


def _extract_body(message: Message) -> str:
    chunks: list[str] = []

    for part in message.walk():
        if part.is_multipart():
            continue

        content_type = part.get_content_type()
        disposition = (
            part.get_content_disposition() or ""
        ).casefold()

        if disposition == "attachment":
            # Nested .eml PEC payloads are handled below.
            if content_type != "message/rfc822":
                continue

        if content_type == "text/plain":
            try:
                text = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                text = payload.decode(
                    part.get_content_charset() or "utf-8",
                    errors="replace",
                )

            text = str(text).strip()
            if text:
                chunks.append(text)

    # Some PEC messages wrap the original message as message/rfc822.
    for part in message.walk():
        if part.get_content_type() != "message/rfc822":
            continue

        payload = part.get_payload()

        nested = (
            payload[0]
            if isinstance(payload, list) and payload
            else None
        )

        if isinstance(nested, Message):
            nested_text = _extract_body(nested)
            if nested_text:
                chunks.append(nested_text)

    return "\n\n".join(dict.fromkeys(chunks))


def _certified(message: Message, subject: str) -> bool | None:
    headers = {
        key.casefold()
        for key in message.keys()
    }

    if (
        "x-ricevuta" in headers
        or "x-trasporto" in headers
        or "posta certificata" in subject.casefold()
    ):
        return True

    return None


def _runts_reference(text: str) -> str | None:
    patterns = (
        # Formato reale delle notifiche RUNTS:
        # [IDPR 2603942]
        r"\bIDPR\s*[:=#]?\s*([A-Za-z0-9_.:/-]{3,240})",

        # Identificativi espliciti eventualmente presenti nei link/body.
        r"\b(?:idComunicazione|communicationId|messageId)"
        r"\s*[=/ :]\s*([A-Za-z0-9_.:-]{3,240})",

        # Forme: RUNTS pratica n. 2603942
        #        pratica n. 2603942
        #        RUNTS id 2603942
        # Il marker 'n' DEVE essere seguito da spazio:
        # così 'RUNTS notification' non diventa 'otification'.
        r"\b(?:RUNTS|pratica)\s+"
        r"(?:pratica\s+)?"
        r"(?:(?:n(?:\.|°|º)?|id)\s+|#\s*)"
        r"([A-Za-z0-9_.:/-]{3,240})",

        # Forma breve numerica: RUNTS 2603942.
        # Solo cifre per non interpretare parole come 'notification'.
        r"\bRUNTS\s+([0-9]{3,240})\b",
    )

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)

    return None

__all__ = [
    "PecImapAdapter",
    "PecImapConfig",
    "PecImapError",
]
