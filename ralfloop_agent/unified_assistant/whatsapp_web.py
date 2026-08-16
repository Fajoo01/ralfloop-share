from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .browser_read_only import BrowserReadOnlyError, CapturedMedia, ax_name
from .whatsapp_navigation import (
    ChatInventoryItem,
    CurrentChat,
    HistoryRead,
    NavigationAudit,
    VisibleMessage,
    WhatsAppCdpNavigator,
)


class WhatsAppWorkProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    profile: Literal["work"] = "work"
    work_profile: Literal[True] = True
    default_namespace: Literal["tiremm"] = "tiremm"
    default_policy: Literal["read"] = "read"
    deny_overrides: tuple[str, ...] = ()


class WhatsAppChatScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Literal["work.default"] = "work.default"
    display_name: Literal["WhatsApp work profile"] = "WhatsApp work profile"
    aliases: tuple[str, ...] = ()
    namespace: Literal["tiremm"] = "tiremm"


class WhatsAppScopeRegistry:
    def __init__(self, profile: WhatsAppWorkProfile) -> None:
        self.profile = profile
        self.scopes = (WhatsAppChatScope(),)

    @classmethod
    def load(cls, path: str | Path) -> "WhatsAppScopeRegistry":
        return cls(WhatsAppWorkProfile.model_validate_json(Path(path).read_text(encoding="utf-8")))

    def resolve(self, _hint: str, *, allowed_namespaces: tuple[str, ...]) -> WhatsAppChatScope | None:
        return WhatsAppChatScope() if self.profile.default_namespace in allowed_namespaces else None

    def chat_allowed(self, chat_id: str) -> bool:
        return chat_id not in set(self.profile.deny_overrides)


class WhatsAppEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    chat_id: str
    author: str = ""
    timestamp: str = ""
    text: str = Field(default="", max_length=6000)
    kind: Literal["text", "image", "audio", "video", "document"] = "text"
    provenance_ref: str
    namespace: Literal["tiremm"] = "tiremm"
    memory_type: Literal["document", "episodic"] = "episodic"
    epistemic_kind: Literal["reported_statement"] = "reported_statement"
    certainty: Literal["reported"] = "reported"
    content_role: Literal["data"] = "data"


class WhatsAppMediaItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_ref: str = Field(pattern=r"^whatsapp_web:wa_chat_[a-f0-9]{16}:media:[a-f0-9]{16}$")
    kind: Literal["image", "audio", "video", "document"]
    mime_type: str = Field(min_length=1, max_length=120)
    byte_count: int = Field(gt=0, le=8 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_message_id: str = Field(default="", pattern=r"^(?:wa_msg_[a-f0-9]{16})?$")
    provenance_ref: str = Field(default="", max_length=240)
    processor_status: Literal[
        "TEXT_EXTRACTED", "NO_TEXT", "TRANSCRIBER_UNAVAILABLE",
        "PROCESSOR_UNAVAILABLE", "MIME_DENIED",
    ]
    extracted_text: str = Field(default="", max_length=12000)
    namespace: Literal["tiremm"] = "tiremm"
    memory_type: Literal["document"] = "document"
    content_role: Literal["data"] = "data"


class WhatsAppSessionStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["AUTHENTICATED", "AUTH_REQUIRED", "SOURCE_UNAVAILABLE"]
    session_authenticated: bool
    accessibility_nodes: int = Field(ge=0)
    read_operations: tuple[str, ...] = ()
    write_operations: int = 0


class WhatsAppSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "FOUND", "NOT_FOUND_IN_SEARCHED_SCOPE", "SEARCH_INCOMPLETE",
        "AUTH_REQUIRED", "SOURCE_UNAVAILABLE",
    ]
    search_complete: bool
    query: str
    chat_id: str = ""
    evidence: tuple[WhatsAppEvidence, ...] = ()
    messages_seen: int = Field(default=0, ge=0)
    oldest_loaded: str = ""
    newest_loaded: str = ""
    scroll_iterations: int = Field(default=0, ge=0)
    history_exhausted: bool = False
    cap_reached: bool = False
    provenance: tuple[str, ...] = ()
    audit: tuple[NavigationAudit, ...] = ()
    writes: int = 0
    sends: int = 0


class WhatsAppReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal[
        "FOUND", "NOT_FOUND_IN_SEARCHED_SCOPE", "SEARCH_INCOMPLETE",
        "AUTH_REQUIRED", "SOURCE_UNAVAILABLE", "AMBIGUOUS_CHAT",
        "CHAT_NOT_FOUND", "POLICY_DENIED",
    ]
    session_authenticated: bool
    namespace: Literal["tiremm"] = "tiremm"
    chat_ref: str | None = None
    visible_items: tuple[str, ...] = Field(default_factory=tuple, max_length=500)
    evidence: tuple[WhatsAppEvidence, ...] = Field(default_factory=tuple, max_length=500)
    media_items: tuple[WhatsAppMediaItem, ...] = Field(default_factory=tuple, max_length=6)
    provenance: tuple[str, ...] = Field(default_factory=tuple, max_length=600)
    read_operations: tuple[str, ...] = Field(default_factory=tuple)
    search_complete: bool = False
    messages_seen: int = Field(default=0, ge=0)
    scroll_iterations: int = Field(default=0, ge=0)
    history_exhausted: bool = False
    cap_reached: bool = False
    write_operations: int = 0
    send_operations: int = 0
    persistent_memory_writes: int = 0
    content_role: Literal["data"] = "data"
    response: str


class MediaInterpreter(Protocol):
    def interpret(self, media: CapturedMedia) -> tuple[str, str]: ...


class AudioTranscriber(Protocol):
    def transcribe(self, content: bytes, mime_type: str) -> str: ...


class LocalWhatsAppMediaInterpreter:
    def __init__(self, transcriber: AudioTranscriber | None = None) -> None:
        self.transcriber = transcriber

    def interpret(self, media: CapturedMedia) -> tuple[str, str]:
        if not _mime_allowed(media.kind, media.mime_type):
            return "MIME_DENIED", ""
        if media.kind == "audio":
            if self.transcriber is None:
                return "TRANSCRIBER_UNAVAILABLE", ""
            text = " ".join(self.transcriber.transcribe(media.content, media.mime_type).split())[:12000]
            return ("TEXT_EXTRACTED", text) if text else ("NO_TEXT", "")
        if media.kind == "video":
            return "PROCESSOR_UNAVAILABLE", ""
        if media.kind == "image":
            return self._run_file_tool(media, "tesseract")
        if media.kind == "document" and media.mime_type == "application/pdf":
            return self._run_file_tool(media, "pdftotext")
        return "PROCESSOR_UNAVAILABLE", ""

    @staticmethod
    def _run_file_tool(media: CapturedMedia, tool: str) -> tuple[str, str]:
        executable = shutil.which(tool)
        if not executable:
            return "PROCESSOR_UNAVAILABLE", ""
        suffix = ".pdf" if media.mime_type == "application/pdf" else ".png"
        path = ""
        try:
            with tempfile.NamedTemporaryFile(prefix="ralf-wa-read-", suffix=suffix, delete=False) as handle:
                path = handle.name
                handle.write(media.content)
                handle.flush()
                os.fsync(handle.fileno())
            argv = (
                [executable, path, "-"]
                if tool == "pdftotext"
                else [executable, path, "stdout", "-l", "ita+eng"]
            )
            completed = subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=45, check=False,
            )
            text = " ".join(completed.stdout.split())[:12000] if completed.returncode == 0 else ""
            return ("TEXT_EXTRACTED", text) if text else ("NO_TEXT", "")
        except (OSError, subprocess.SubprocessError):
            return "PROCESSOR_UNAVAILABLE", ""
        finally:
            if path:
                Path(path).unlink(missing_ok=True)


class WhatsAppWebReadOnly:
    def __init__(
        self,
        navigator: WhatsAppCdpNavigator,
        scopes: WhatsAppScopeRegistry,
        media_interpreter: MediaInterpreter | None = None,
    ) -> None:
        self.navigator = navigator
        self.scopes = scopes
        self.media_interpreter = media_interpreter or LocalWhatsAppMediaInterpreter()
        self._chat_titles: dict[str, str] = {}

    @classmethod
    def from_environment(cls) -> "WhatsAppWebReadOnly":
        root = Path(__file__).resolve().parents[2]
        scope_path = os.getenv(
            "RALFLOOP_WHATSAPP_SCOPE_REGISTRY",
            str(root / "config" / "whatsapp_memory_scopes_v1.json"),
        )
        endpoint = os.getenv("RALFLOOP_WHATSAPP_CDP_ENDPOINT", "http://127.0.0.1:9236")
        max_scroll = _env_int("RALFLOOP_WHATSAPP_MAX_SCROLL_ITERATIONS", 8, 1, 64)
        wait = _env_float("RALFLOOP_WHATSAPP_NAVIGATION_WAIT_SECONDS", 0.6, 0.0, 3.0)
        return cls(
            WhatsAppCdpNavigator.from_endpoint(
                endpoint, wait_seconds=wait, max_scroll_iterations=max_scroll,
            ),
            WhatsAppScopeRegistry.load(scope_path),
        )

    def inspect_session(self) -> WhatsAppSessionStatus:
        try:
            snapshot = self.navigator.client.snapshot(expected_host="web.whatsapp.com")
        except BrowserReadOnlyError:
            return WhatsAppSessionStatus(
                status="SOURCE_UNAVAILABLE", session_authenticated=False, accessibility_nodes=0,
            )
        names = tuple(ax_name(node).casefold() for node in snapshot.nodes if ax_name(node))
        login_required = any(
            term in name for name in names
            for term in ("codice qr", "qr code", "collega un dispositivo", "link a device")
        )
        authenticated = not login_required and any(
            term in name for name in names
            for term in ("cerca o avvia una nuova chat", "search or start a new chat")
        )
        if not login_required and not authenticated:
            try:
                authenticated = bool(self.navigator.list_chats()) or self.navigator.read_current_chat().status == "FOUND"
            except BrowserReadOnlyError:
                authenticated = False
        return WhatsAppSessionStatus(
            status="AUTHENTICATED" if authenticated else "AUTH_REQUIRED",
            session_authenticated=authenticated,
            accessibility_nodes=len(snapshot.nodes), read_operations=snapshot.operations,
        )

    def list_chats(self) -> tuple[ChatInventoryItem, ...]:
        rows = tuple(item for item in self.navigator.list_chats() if self.scopes.chat_allowed(item.chat_id))
        self._chat_titles.update({item.chat_id: item.title for item in rows})
        return rows

    def search_chats(self, query: str) -> tuple[ChatInventoryItem, ...]:
        folded = _normalize(query)
        if not folded:
            return ()
        rows = self.list_chats()
        matches = tuple(item for item in rows if folded in _normalize(item.title))
        if matches:
            return matches
        ui_rows = self.navigator.search_global(query)
        result: list[ChatInventoryItem] = []
        for row in ui_rows:
            title = str(row.get("title") or "")
            if not title:
                continue
            chat_id = _chat_id(title)
            if self.scopes.chat_allowed(chat_id):
                result.append(ChatInventoryItem(
                    chat_id=chat_id, title=title,
                    secondary=str(row.get("excerpt") or "")[:1000],
                ))
        self._chat_titles.update({item.chat_id: item.title for item in result})
        return tuple(result)

    def open_chat(self, chat_ref: str) -> dict[str, Any]:
        title = self._resolve_chat_title(chat_ref)
        if not title:
            return {"status": "CHAT_NOT_FOUND", "candidates": []}
        chat_id = _chat_id(title)
        if not self.scopes.chat_allowed(chat_id):
            return {"status": "POLICY_DENIED", "candidates": []}
        opener = getattr(self.navigator, "locate_and_open_chat", self.navigator.open_chat)
        return opener(title)

    def read_messages(self, chat_ref: str = "", *, limit: int = 100) -> CurrentChat:
        if not 1 <= limit <= 500:
            raise ValueError("whatsapp_message_limit_invalid")
        if chat_ref:
            opened = self.open_chat(chat_ref)
            if opened.get("status") != "OPENED":
                return CurrentChat(status="NO_CHAT")
        current = self.navigator.read_current_chat()
        return current.model_copy(update={"messages": current.messages[-limit:]})

    def read_history(
        self, chat_ref: str, *, limit: int = 200, max_scroll_iterations: int | None = None,
    ) -> HistoryRead:
        if not 1 <= limit <= 500:
            raise ValueError("whatsapp_message_limit_invalid")
        opened = self.open_chat(chat_ref)
        if opened.get("status") != "OPENED":
            return HistoryRead(
                chat=CurrentChat(status="NO_CHAT"), messages_seen=0,
                scroll_iterations=0, history_exhausted=False, cap_reached=False, audit=(),
            )
        history = self.navigator.scroll_history(max_iterations=max_scroll_iterations)
        return history.model_copy(update={
            "chat": history.chat.model_copy(update={"messages": history.chat.messages[-limit:]}),
            "messages_seen": min(history.messages_seen, limit),
        })

    def search_messages(
        self,
        query: str,
        *,
        chat_ref: str = "",
        limit: int = 100,
        max_scroll_iterations: int | None = None,
    ) -> WhatsAppSearchResult:
        tokens = _query_tokens(query)
        if not tokens:
            raise ValueError("whatsapp_search_query_required")
        if chat_ref:
            history = self.read_history(
                chat_ref, limit=min(limit * 5, 500),
                max_scroll_iterations=max_scroll_iterations,
            )
            evidence = tuple(
                _evidence(item, history.chat.chat_id)
                for item in history.chat.messages if _matches(item.text, tokens)
            )[:limit]
            complete = history.history_exhausted and not history.cap_reached
            status = "FOUND" if evidence and complete else (
                "SEARCH_INCOMPLETE" if not complete else "NOT_FOUND_IN_SEARCHED_SCOPE"
            )
            return WhatsAppSearchResult(
                status=status, search_complete=complete, query=query,
                chat_id=history.chat.chat_id, evidence=evidence,
                messages_seen=history.messages_seen,
                oldest_loaded=history.chat.oldest_loaded,
                newest_loaded=history.chat.newest_loaded,
                scroll_iterations=history.scroll_iterations,
                history_exhausted=history.history_exhausted,
                cap_reached=history.cap_reached,
                provenance=tuple(item.provenance_ref for item in evidence),
                audit=history.audit,
            )
        ui_rows = self.navigator.search_global(" ".join(tokens)[:200])
        evidence: list[WhatsAppEvidence] = []
        for index, row in enumerate(ui_rows[:limit]):
            title = str(row.get("title") or "")
            excerpt = str(row.get("excerpt") or "")
            if not title:
                continue
            chat_id = _chat_id(title)
            digest = hashlib.sha256(f"{chat_id}\0{excerpt}\0{index}".encode()).hexdigest()[:16]
            evidence.append(WhatsAppEvidence(
                message_id=f"wa_msg_{digest}", chat_id=chat_id,
                text=excerpt, provenance_ref=f"whatsapp_web:{chat_id}:search:{digest}",
            ))
        return WhatsAppSearchResult(
            status="SEARCH_INCOMPLETE", search_complete=False, query=query,
            evidence=tuple(evidence), messages_seen=len(evidence),
            provenance=tuple(item.provenance_ref for item in evidence),
            audit=tuple(self.navigator.audit_events),
        )

    def get_chat_info(self, chat_ref: str) -> dict[str, Any]:
        opened = self.open_chat(chat_ref)
        if opened.get("status") != "OPENED":
            return {"status": opened.get("status"), "text": "", "namespace": "tiremm"}
        return {**self.navigator.read_chat_info(), "namespace": "tiremm", "content_role": "data"}

    def get_media(self, chat_ref: str, message_id: str) -> tuple[WhatsAppMediaItem, ...]:
        current = self.read_messages(chat_ref, limit=500)
        message = next((item for item in current.messages if item.message_id == message_id), None)
        if message is None or message.kind not in {"image", "audio", "video", "document"}:
            return ()
        opened_viewer = False
        try:
            exact_reader = getattr(self.navigator.client, "whatsapp_message_media", None)
            captured = exact_reader(message.raw_ref) if exact_reader is not None else ()
            if not captured:
                if message.kind == "image":
                    opened = self.navigator.open_media(message.raw_ref)
                    if opened not in {"NAVIGATED", "NOT_FOUND"}:
                        return ()
                    opened_viewer = opened == "NAVIGATED"
                captured = self.navigator.client.visible_whatsapp_media()
        except BrowserReadOnlyError:
            captured = ()
        rows = tuple(
            self._media_item(current.chat_id, message.message_id, media)
            for media in captured[:6] if media.kind == message.kind
        )
        if opened_viewer:
            self.navigator.close_read_panel()
        return rows

    def read(
        self,
        request: str,
        *,
        allowed_namespaces: tuple[str, ...],
        last_chat_ref: str | None = None,
    ) -> WhatsAppReadResult:
        scope = self.scopes.resolve(request, allowed_namespaces=allowed_namespaces)
        if scope is None:
            return _read_result("POLICY_DENIED", False, "Namespace WhatsApp non autorizzato.")
        session = self.inspect_session()
        if session.status != "AUTHENTICATED":
            return _read_result(session.status, False, "Sessione WhatsApp Web non disponibile o non autenticata.")
        chat_hint = _extract_chat_hint(request) or (last_chat_ref if _uses_conversation_reference(request) else "")
        query = _extract_query(request.replace(chat_hint, "", 1) if chat_hint else request)
        if chat_hint:
            chats = self.search_chats(chat_hint)
            if len(chats) > 1:
                return _read_result("AMBIGUOUS_CHAT", True, "Più chat compatibili; specifica quale.")
            if not chats:
                return _read_result("CHAT_NOT_FOUND", True, "Chat WhatsApp non trovata nello scope visibile.")
            search = self.search_messages(query or chat_hint, chat_ref=chats[0].chat_id)
        else:
            search = self.search_messages(query or request)
        evidence = search.evidence
        media: tuple[WhatsAppMediaItem, ...] = ()
        if _media_requested(request) and search.chat_id:
            current = self.read_messages(search.chat_id, limit=100)
            target = next((item for item in reversed(current.messages) if item.kind != "text"), None)
            if target:
                media = self.get_media(search.chat_id, target.message_id)
        texts = tuple(item.text for item in evidence if item.text)
        for item in media:
            if item.extracted_text:
                texts = (*texts, item.extracted_text)
        return WhatsAppReadResult(
            status=search.status,
            session_authenticated=True,
            chat_ref=search.chat_id or None,
            visible_items=texts,
            evidence=evidence,
            media_items=media,
            provenance=tuple((*search.provenance, *(item.artifact_ref for item in media))),
            read_operations=tuple(item.operation for item in search.audit),
            search_complete=search.search_complete,
            messages_seen=search.messages_seen,
            scroll_iterations=search.scroll_iterations,
            history_exhausted=search.history_exhausted,
            cap_reached=search.cap_reached,
            response=_search_response(search, len(media)),
        )

    def _resolve_chat_title(self, chat_ref: str) -> str:
        if chat_ref.startswith("wa_chat_"):
            if chat_ref not in self._chat_titles:
                self.list_chats()
            return self._chat_titles.get(chat_ref, "")
        matches = self.search_chats(chat_ref)
        return matches[0].title if len(matches) == 1 else ""

    def _media_item(
        self, chat_id: str, message_id: str, media: CapturedMedia,
    ) -> WhatsAppMediaItem:
        full_digest = hashlib.sha256(media.content).hexdigest()
        status, text = self.media_interpreter.interpret(media)
        return WhatsAppMediaItem(
            artifact_ref=f"whatsapp_web:{chat_id}:media:{full_digest[:16]}",
            kind=media.kind, mime_type=media.mime_type,
            byte_count=len(media.content), sha256=full_digest,
            source_message_id=message_id,
            provenance_ref=f"whatsapp_web:{chat_id}:message:{message_id.removeprefix('wa_msg_')}",
            processor_status=status, extracted_text=text,
        )


def _evidence(message: VisibleMessage, chat_id: str) -> WhatsAppEvidence:
    return WhatsAppEvidence(
        message_id=message.message_id, chat_id=chat_id,
        author=message.author, timestamp=message.timestamp,
        text=message.text, kind=message.kind,
        provenance_ref=message.provenance_ref,
    )


def _read_result(status: str, authenticated: bool, response: str) -> WhatsAppReadResult:
    return WhatsAppReadResult(
        status=status, session_authenticated=authenticated, response=response,
    )


def _search_response(result: WhatsAppSearchResult, media_count: int) -> str:
    if result.status == "FOUND":
        return f"Trovati {len(result.evidence)} messaggi WhatsApp nello scope esplorato; media acquisiti: {media_count}."
    if result.status == "NOT_FOUND_IN_SEARCHED_SCOPE":
        return "Nessun messaggio trovato nello scope WhatsApp completamente esplorato."
    return (
        f"Ricerca WhatsApp incompleta: {len(result.evidence)} risultati nello scope caricato; "
        "non è possibile concludere che non se ne sia mai parlato."
    )


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _query_tokens(value: str) -> tuple[str, ...]:
    stop = {
        "cerca", "trova", "controlla", "whatsapp", "messaggi", "messaggio", "chat",
        "cosa", "aveva", "avevamo", "detto", "scritto", "quello", "quella", "sul",
        "sulla", "nel", "nella", "con", "tra", "mail", "email", "dimmi", "fammi",
    }
    tokens = [item for item in re.findall(r"[\wÀ-ÿ]{3,}", value.casefold()) if item not in stop]
    return tuple(dict.fromkeys(tokens))[:8]


def _matches(text: str, tokens: tuple[str, ...]) -> bool:
    folded = _normalize(text)
    return bool(tokens) and all(token in folded for token in tokens)


def _extract_query(request: str) -> str:
    tokens = _query_tokens(request)
    return " ".join(tokens)


def _extract_chat_hint(request: str) -> str:
    patterns = (
        r"\b(?:conversazione|chat)\s+con\s+([\wÀ-ÿ'. -]{2,80}?)(?=\s+(?:su|sul|sulla|del|della|che|e)\b|[?.!,]|$)",
        r"\b(?:gruppo)\s+(?:del|della|di)?\s*([\wÀ-ÿ'. -]{2,80}?)(?=\s+(?:su|sul|che|e)\b|[?.!,]|$)",
        r"\b(?:detto|scritto|mandato|audio)\s+([A-ZÀ-Ý][\wÀ-ÿ'.-]{1,60})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, request)
        if match:
            return " ".join(match.group(1).split())
    return ""


def _uses_conversation_reference(request: str) -> bool:
    return bool(re.search(r"\b(?:quella chat|quel gruppo|quello che|l'audio di ieri)\b", request, re.I))


def _media_requested(request: str) -> bool:
    return bool(re.search(r"\b(?:audio|vocale|immagine|foto|media|documento|allegato|pdf)\b", request, re.I))


def _mime_allowed(kind: str, mime: str) -> bool:
    return {
        "image": mime.startswith("image/"),
        "audio": mime.startswith("audio/"),
        "video": mime.startswith("video/"),
        "document": mime in {
            "application/pdf", "text/plain", "application/octet-stream",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        },
    }.get(kind, False)


def _chat_id(title: str) -> str:
    return "wa_chat_" + hashlib.sha256(title.casefold().encode()).hexdigest()[:16]


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(os.getenv(name, str(default))), high))
    except ValueError:
        return default


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(float(os.getenv(name, str(default))), high))
    except ValueError:
        return default


__all__ = [
    "LocalWhatsAppMediaInterpreter", "WhatsAppChatScope", "WhatsAppEvidence",
    "WhatsAppMediaItem", "WhatsAppReadResult", "WhatsAppScopeRegistry",
    "WhatsAppSearchResult", "WhatsAppSessionStatus", "WhatsAppWebReadOnly",
    "WhatsAppWorkProfile",
]
