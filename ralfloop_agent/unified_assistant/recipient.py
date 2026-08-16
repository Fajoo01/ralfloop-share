from __future__ import annotations

from email.utils import parseaddr
import os
import re
from typing import Any, Callable, Mapping, Protocol

from src.google_workspace import GoogleWorkspaceGateway
from src.mcp_transport import MCPClientSession, UnixMCPTransport


_EMAIL_RE = re.compile(r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.I)


class GmailReadGateway(Protocol):
    account: str

    def invoke(self, operation: str, **arguments: Any) -> Mapping[str, Any]: ...


class GenericRecipientResolver:
    """Explicit address -> current thread -> verified Gmail participants."""

    def __init__(
        self,
        gateway_factory: Callable[[], Any],
        *,
        account: str,
        current_thread: Mapping[str, Any] | None = None,
        max_results: int = 8,
    ) -> None:
        self.gateway_factory = gateway_factory
        self.account = account.casefold()
        self.current_thread = dict(current_thread or {})
        self.max_results = max(1, min(int(max_results), 20))

    def resolve(self, label: str) -> dict[str, Any] | None:
        normalized = " ".join(label.split()).strip(" <>\t\r\n")
        _, explicit = parseaddr(normalized)
        if explicit and _EMAIL_RE.fullmatch(explicit):
            return {
                "status": "resolved", "name": parseaddr(normalized)[0] or explicit,
                "address": explicit.casefold(), "source": "explicit_instruction",
            }
        thread_candidates = _thread_candidates(self.current_thread, normalized, self.account)
        if len(thread_candidates) == 1:
            return _resolved(thread_candidates[0], source="current_thread", thread=self.current_thread)
        if len(thread_candidates) > 1:
            return _ambiguous(thread_candidates, "current_thread")
        with self.gateway_factory() as gateway:
            candidates = self._gmail_candidates(gateway, normalized)
            if len(candidates) != 1:
                return _ambiguous(candidates, "gmail_participants") if candidates else None
            candidate = candidates[0]
            source_email: dict[str, Any] = {}
            message_id = str(candidate.get("message_id") or "")
            if message_id:
                try:
                    detail = gateway.invoke("read", messageId=message_id)
                    message = detail.get("message") if isinstance(detail, Mapping) else None
                    if isinstance(message, Mapping):
                        thread_id = str(message.get("threadId") or "")
                        subject = str(message.get("subject") or candidate.get("subject") or "")
                        if not thread_id and subject:
                            thread_id = _resolve_thread_id(gateway, subject)
                        source_email = {
                            "sender": str(message.get("from") or candidate["address"]),
                            "reply_to": candidate["address"],
                            "subject": subject,
                            "date": str(message.get("date") or ""),
                            "message_id": str(message.get("messageId") or message_id),
                            "thread_id": thread_id,
                            "body": str(message.get("body") or ""),
                        }
                        if thread_id:
                            try:
                                thread = gateway.invoke("getThread", threadId=thread_id)
                                source_email["thread_context"] = _thread_context(thread)
                            except Exception:
                                source_email["thread_context"] = []
                except Exception:
                    source_email = {}
            return _resolved(candidate, source="gmail_verified_participant", source_email=source_email)

    def resolve_exact_reply(
        self,
        address: str,
        message_id: str,
    ) -> dict[str, Any] | None:
        """Resolve an exact Gmail reply source; fail closed on identity mismatch."""

        _, parsed = parseaddr(address)
        wanted = parsed.casefold().strip()
        source_id = message_id.casefold().strip()

        if not wanted or not _EMAIL_RE.fullmatch(wanted):
            return None
        if not re.fullmatch(r"[0-9a-f]{16,64}", source_id):
            return None

        with self.gateway_factory() as gateway:
            try:
                detail = gateway.invoke("read", messageId=source_id)
            except Exception:
                return None

            message = detail.get("message") if isinstance(detail, Mapping) else None
            if not isinstance(message, Mapping):
                return None

            observed_message_id = str(message.get("messageId") or "").casefold()
            display, observed_address = parseaddr(str(message.get("from") or ""))
            observed_address = observed_address.casefold()

            if observed_message_id != source_id:
                return {
                    "status": "mismatch",
                    "reason": "source_message_id_mismatch",
                }

            if observed_address != wanted:
                return {
                    "status": "mismatch",
                    "reason": "source_sender_mismatch",
                }

            subject = str(message.get("subject") or "")
            thread_id = str(message.get("threadId") or "")
            thread_context: list[dict[str, Any]] = []

            # Gmail commonly allows the first message ID as the thread ID.
            # We never assume that: use it only if getThread confirms it.
            if not thread_id:
                try:
                    thread = gateway.invoke("getThread", threadId=source_id)
                except Exception:
                    thread = None

                if isinstance(thread, Mapping):
                    confirmed_thread_id = str(thread.get("threadId") or "")
                    if confirmed_thread_id:
                        thread_id = confirmed_thread_id
                        thread_context = _thread_context(thread)

            # Fallback only through a provider query on the observed subject.
            if not thread_id and subject:
                thread_id = _resolve_thread_id(gateway, subject)

            if thread_id and not thread_context:
                try:
                    thread = gateway.invoke("getThread", threadId=thread_id)
                    if isinstance(thread, Mapping):
                        thread_context = _thread_context(thread)
                except Exception:
                    thread_context = []

            if not thread_id:
                return {
                    "status": "mismatch",
                    "reason": "source_thread_unresolved",
                }

            source_email = {
                "sender": str(message.get("from") or observed_address),
                "reply_to": wanted,
                "subject": subject,
                "date": str(message.get("date") or ""),
                "message_id": observed_message_id,
                "thread_id": thread_id,
                "body": str(message.get("body") or ""),
                "thread_context": thread_context,
            }

            return {
                "status": "resolved",
                "name": display or wanted.split("@", 1)[0],
                "address": wanted,
                "source": "explicit_message_verified",
                "subject": subject,
                "source_email": source_email,
                "thread_context": thread_context,
            }

    def _gmail_candidates(self, gateway: GmailReadGateway, label: str) -> list[dict[str, str]]:
        result = gateway.invoke("search", query=f'from:("{_query(label)}")', maxResults=self.max_results)
        rows = result.get("messages") if isinstance(result, Mapping) else None
        if not rows:
            result = gateway.invoke("search", query=f'"{_query(label)}"', maxResults=self.max_results)
            rows = result.get("messages") if isinstance(result, Mapping) else None
        candidates: dict[str, dict[str, str]] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, Mapping):
                continue
            message_id = str(row.get("messageId") or "")
            sender = str(row.get("sender") or row.get("from") or "")
            subject = str(row.get("subject") or "")
            display, address = parseaddr(sender)
            if message_id and (not _EMAIL_RE.fullmatch(address) or not _matches(label, display, address)):
                try:
                    detail = gateway.invoke("read", messageId=message_id)
                    message = detail.get("message") if isinstance(detail, Mapping) else None
                    if isinstance(message, Mapping):
                        display, address = parseaddr(str(message.get("from") or ""))
                        subject = str(message.get("subject") or subject)
                except Exception:
                    pass
            if not address or address.casefold() == self.account or not _matches(label, display, address):
                continue
            candidates.setdefault(address.casefold(), {
                "name": display or address.split("@", 1)[0],
                "address": address.casefold(),
                "message_id": message_id,
                "subject": subject,
            })
        return sorted(candidates.values(), key=lambda item: (item["name"].casefold(), item["address"]))


class GoogleWorkspaceRecipientResolver(GenericRecipientResolver):
    @classmethod
    def from_environment(cls, *, current_thread: Mapping[str, Any] | None = None) -> "GoogleWorkspaceRecipientResolver":
        socket_path = os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
        account = os.getenv("RALF_GOOGLE_WORKSPACE_ACCOUNT", "fabio@tiremminnanz.com")
        timeout = float(os.getenv("RALF_GOOGLE_WORKSPACE_MCP_TIMEOUT", "20"))

        class _Context:
            def __enter__(self) -> GoogleWorkspaceGateway:
                self.session = MCPClientSession(UnixMCPTransport(socket_path), timeout=timeout)
                self.session.__enter__()
                self.gateway = GoogleWorkspaceGateway(self.session, account=account)
                self.gateway.discover()
                return self.gateway

            def __exit__(self, exc_type, exc, tb) -> None:
                self.session.__exit__(exc_type, exc, tb)

        return cls(_Context, account=account, current_thread=current_thread)


def _thread_candidates(thread: Mapping[str, Any], label: str, account: str) -> list[dict[str, str]]:
    values: list[str] = []
    for key in ("sender", "from", "reply_to", "to", "cc", "participants"):
        value = thread.get(key)
        if isinstance(value, str):
            values.append(value)
        elif isinstance(value, (list, tuple)):
            values.extend(str(item) for item in value)
    result: dict[str, dict[str, str]] = {}
    for display, address in (parseaddr(value) for value in values):
        if address and address.casefold() != account and _matches(label, display, address):
            result[address.casefold()] = {"name": display or address, "address": address.casefold()}
    return list(result.values())


def _matches(label: str, display: str, address: str) -> bool:
    wanted = set(_words(label))
    local, _, domain = address.partition("@")
    identity_words = set(_words(display + " " + local))
    domain_words = set(_words(domain))
    return bool(wanted) and all(
        word in identity_words
        or any(word == token or len(word) >= 4 and token.endswith(word) for token in domain_words)
        for word in wanted
    )


def _words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9à-ÿ]+", value.casefold())


def _query(value: str) -> str:
    return value.replace("\\", " ").replace('"', " ")[:120]


def _resolve_thread_id(gateway: GmailReadGateway, subject: str) -> str:
    try:
        result = gateway.invoke("threads", query=f'subject:"{_query(subject)}"', maxResults=5)
    except Exception:
        return ""
    rows = result.get("threads") if isinstance(result, Mapping) else None
    ids = {
        str(item.get("threadId") or item.get("id") or "")
        for item in rows if isinstance(item, Mapping)
    } if isinstance(rows, list) else set()
    ids.discard("")
    return next(iter(ids)) if len(ids) == 1 else ""


def _thread_context(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = value.get("messages") if isinstance(value, Mapping) else None
    result: list[dict[str, Any]] = []
    for item in rows if isinstance(rows, list) else []:
        if not isinstance(item, Mapping):
            continue
        result.append({
            "sender": str(item.get("from") or item.get("sender") or ""),
            "subject": str(item.get("subject") or ""),
            "date": str(item.get("date") or ""),
            "body": str(item.get("body") or "")[:4000],
        })
    return result[:24]


def _resolved(candidate: Mapping[str, Any], *, source: str, **extra: Any) -> dict[str, Any]:
    return {"status": "resolved", **dict(candidate), "source": source, **extra}


def _ambiguous(candidates: list[dict[str, str]], source: str) -> dict[str, Any]:
    return {
        "status": "ambiguous", "source": source,
        "candidates": [
            {"name": item.get("name") or "", "address_hint": _address_hint(item.get("address") or "")}
            for item in candidates[:8]
        ],
    }


def _address_hint(address: str) -> str:
    local, _, domain = address.partition("@")
    return ((local[:2] + "…") if local else "…") + ("@" + domain if domain else "")


__all__ = ["GenericRecipientResolver", "GoogleWorkspaceRecipientResolver", "GmailReadGateway"]
