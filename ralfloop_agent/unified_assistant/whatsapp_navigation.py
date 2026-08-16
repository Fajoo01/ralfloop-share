from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .browser_read_only import BrowserReadOnlyError, CdpReadOnlySnapshotClient


_LIST_CHATS_SCRIPT = r"""
(() => Array.from(document.querySelectorAll('#pane-side [role="row"]')).filter((row) => row.querySelector('[data-testid="cell-frame-container"]')).slice(0, 200).map((row) => {
  const lines = String(row.innerText || '').split('\n').map((value) => value.trim()).filter(Boolean);
  const titleNode = row.querySelector('[data-testid="cell-frame-title"],[title],[dir="auto"]');
  const title = String(titleNode && (titleNode.getAttribute('title') || titleNode.textContent) || lines[0] || '').trim();
  return {
    title: title.slice(0, 240),
    secondary: String(lines.slice(1).join(' | ')).slice(0, 1000),
    ui_ref: String(row.getAttribute('data-testid') || '').slice(0, 96)
  };
}))()
""".strip()


_READ_CURRENT_CHAT_SCRIPT = r"""
(() => {
  const main = document.querySelector('#main');
  if (!main) return {status: 'NO_CHAT', title: '', participants: '', messages: [], scroll: null};
  const header = main.querySelector('header');
  const titleRoot = header && header.querySelector('[data-testid="conversation-info-header"]');
  const titleCandidate = titleRoot || (header && header.querySelector('[title],[dir="auto"]'));
  const titleText = String(titleCandidate && (titleCandidate.getAttribute('title') || titleCandidate.innerText || titleCandidate.textContent) || '').trim();
  const headerLines = String((header || {}).innerText || '').split('\n').map((value) => value.trim()).filter(Boolean);
  const candidates = Array.from(main.querySelectorAll('*')).filter((element) =>
    element.scrollHeight > element.clientHeight + 100 && element.clientHeight > 160
  ).sort((left, right) => right.scrollHeight - left.scrollHeight);
  const scroller = candidates[0] || null;
  const seen = new Set();
  const messages = [];
  for (const node of Array.from(main.querySelectorAll('[data-id]'))) {
    const rawId = String(node.getAttribute('data-id') || '');
    if (!rawId || seen.has(rawId)) continue;
    seen.add(rawId);
    const plain = node.querySelector('[data-pre-plain-text]');
    const metadata = String(plain && plain.getAttribute('data-pre-plain-text') || '').slice(0, 500);
    const textRoot = node.querySelector('[data-testid="msg-text"],[data-pre-plain-text]');
    const text = String((textRoot && textRoot.innerText) || node.innerText || '').trim().slice(0, 6000);
    const kind = node.querySelector('audio') ? 'audio' :
      node.querySelector('video') ? 'video' :
      node.querySelector('a[download],[data-testid*="document"],[data-icon="document-PDF-icon"]') ? 'document' :
      node.querySelector('img,[data-testid="media-image"]') ? 'image' : 'text';
    messages.push({
      raw_id: rawId.slice(0, 300), metadata, text, kind,
      outbound: /(?:^|_)true(?:_|$)/.test(rawId) ||
        node.classList.contains('message-out') || Boolean(node.closest('.message-out')) ||
        Boolean(node.querySelector('[data-icon="msg-dblcheck"],[data-icon="msg-check"]'))
    });
  }
  return {
    status: 'FOUND', title: String(titleText || headerLines[0] || '').slice(0, 240),
    participants: String(headerLines.filter((line) => line !== titleText).join(' | ')).slice(0, 1000), messages: messages.slice(-500),
    scroll: scroller ? {top: scroller.scrollTop, height: scroller.scrollHeight, client: scroller.clientHeight} : null
  };
})()
""".strip()


_LIST_SEARCH_RESULTS_SCRIPT = r"""
(() => Array.from(document.querySelectorAll('#pane-side [role="row"]')).filter((row) => row.querySelector('[data-testid="cell-frame-container"]') && row.getBoundingClientRect().height > 0).slice(0, 200).map((row) => {
  const lines = String(row.innerText || '').split('\n').map((value) => value.trim()).filter(Boolean);
  const titleNode = row.querySelector('[data-testid="cell-frame-title"],[title],[dir="auto"]');
  const title = String(titleNode && (titleNode.getAttribute('title') || titleNode.textContent) || lines[0] || '').trim();
  return {title: title.slice(0, 240), excerpt: String(lines.filter((line) => line !== title).join(' | ')).slice(0, 1200)};
}))()
""".strip()


_READ_CHAT_INFO_SCRIPT = r"""
(() => {
  const drawer = document.querySelector('[data-testid="drawer-right"],[data-testid="drawer-middle"]');
  if (!drawer) return {status: 'NOT_OPEN', text: ''};
  return {status: 'FOUND', text: String(drawer.innerText || '').trim().slice(0, 12000)};
})()
""".strip()


_OPEN_CHAT_FUNCTION = r"""
function(query) {
  const normalize = (value) => String(value || '').normalize('NFKC').toLocaleLowerCase('it').replace(/\s+/g, ' ').trim();
  const wanted = normalize(query);
  if (!wanted) return {status: 'INVALID', candidates: []};
  const rows = Array.from(document.querySelectorAll('#pane-side [role="row"]')).filter((row) => row.querySelector('[data-testid="cell-frame-container"]')).map((row) => {
    const titleNode = row.querySelector('[data-testid="cell-frame-title"],[title],[dir="auto"]');
    const title = String(titleNode && (titleNode.getAttribute('title') || titleNode.textContent) || String(row.innerText || '').split('\n')[0] || '').trim();
    return {row, titleNode, title, folded: normalize(title)};
  }).filter((item) => item.title);
  let matches = rows.filter((item) => item.folded === wanted);
  if (!matches.length) matches = rows.filter((item) => item.folded.includes(wanted));
  if (matches.length !== 1) return {status: matches.length ? 'AMBIGUOUS' : 'NOT_FOUND', candidates: matches.slice(0, 8).map((item) => item.title)};
  const target = matches[0].row;
  if (target.getAttribute('role') !== 'row' || !target.closest('#pane-side')) return {status: 'DENIED', candidates: []};
  target.scrollIntoView({block: 'nearest'});
  const control = target.querySelector('[data-testid="cell-frame-container"]');
  if (!control) return {status: 'DENIED', candidates: []};
  for (const type of ['pointerdown','mousedown','pointerup','mouseup','click']) {
    const EventType = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
    control.dispatchEvent(new EventType(type, {bubbles: true, cancelable: true, view: window, button: 0}));
  }
  return {status: 'NAVIGATED', title: matches[0].title, candidates: []};
}
""".strip()


_FOCUS_GLOBAL_SEARCH_FUNCTION = r"""
function() {
  const input = document.querySelector('#side input[role="textbox"][data-tab="3"]');
  if (!input || !input.closest('#side')) return {status: 'DENIED'};
  input.focus(); input.select();
  return {status: 'ARMED'};
}
""".strip()


_CLEAR_GLOBAL_SEARCH_FUNCTION = r"""
function() {
  const input = document.querySelector('#side input[role="textbox"][data-tab="3"]');
  if (!input || !input.closest('#side')) return {status: 'DENIED'};
  if (input.value === '') return {status: 'ALREADY_CLEAR'};
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, '');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  return {status: input.value === '' ? 'CLEARED' : 'VERIFICATION_FAILED'};
}
""".strip()


_SCROLL_HISTORY_FUNCTION = r"""
function() {
  const main = document.querySelector('#main');
  if (!main) return {status: 'NO_CHAT'};
  const candidates = Array.from(main.querySelectorAll('*')).filter((element) =>
    element.scrollHeight > element.clientHeight + 100 && element.clientHeight > 160
  ).sort((left, right) => right.scrollHeight - left.scrollHeight);
  const scroller = candidates[0];
  if (!scroller) return {status: 'NO_SCROLLER'};
  const before = {top: scroller.scrollTop, height: scroller.scrollHeight, client: scroller.clientHeight};
  scroller.scrollTop = 0;
  return {status: 'SCROLLED', before, after: {top: scroller.scrollTop, height: scroller.scrollHeight, client: scroller.clientHeight}};
}
""".strip()


_OPEN_CHAT_INFO_FUNCTION = r"""
function() {
  const control = document.querySelector('#main header [data-testid="conversation-info-header"]');
  if (!control || !control.closest('#main header')) return {status: 'NOT_FOUND'};
  control.click();
  return {status: 'NAVIGATED'};
}
""".strip()


_OPEN_MEDIA_FUNCTION = r"""
function(messageId) {
  const main = document.querySelector('#main');
  if (!main) return {status: 'NO_CHAT'};
  const messages = Array.from(main.querySelectorAll('[data-id]'));
  const message = messageId ? messages.find((item) => item.getAttribute('data-id') === messageId) : null;
  const root = message || main;
  const media = Array.from(root.querySelectorAll('[data-testid="media-image"],img')).filter((item) => {
    const box = item.getBoundingClientRect(); return box.width > 40 && box.height > 40;
  });
  if (media.length !== 1) return {status: media.length ? 'AMBIGUOUS' : 'NOT_FOUND'};
  const control = media[0].closest('[role="button"]') || media[0];
  const label = String(control.getAttribute('aria-label') || control.getAttribute('title') || '').toLocaleLowerCase('it');
  const denied = ['invia','send','elimina','delete','modifica','edit','inoltra','forward','reazione','reaction'];
  if (denied.some((term) => label.includes(term))) return {status: 'DENIED'};
  control.click();
  return {status: 'NAVIGATED'};
}
""".strip()


EXPRESSIONS = frozenset({
    _LIST_CHATS_SCRIPT, _READ_CURRENT_CHAT_SCRIPT,
    _LIST_SEARCH_RESULTS_SCRIPT, _READ_CHAT_INFO_SCRIPT,
})
FUNCTIONS = frozenset({
    _OPEN_CHAT_FUNCTION, _FOCUS_GLOBAL_SEARCH_FUNCTION, _CLEAR_GLOBAL_SEARCH_FUNCTION, _SCROLL_HISTORY_FUNCTION,
    _OPEN_CHAT_INFO_FUNCTION, _OPEN_MEDIA_FUNCTION,
})


class NavigationAudit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    classification: Literal["READ", "READ_NAVIGATION", "DENY"]
    target_ref: str = ""
    inspected_count: int = Field(default=0, ge=0)
    artifacts_acquired: int = Field(default=0, ge=0)
    writes: int = 0
    sends: int = 0


class ChatInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chat_id: str = Field(pattern=r"^wa_chat_[a-f0-9]{16}$")
    title: str = Field(min_length=1, max_length=240)
    secondary: str = Field(default="", max_length=1000)
    namespace: Literal["tiremm"] = "tiremm"
    source_type: Literal["document"] = "document"


class VisibleMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str = Field(pattern=r"^wa_msg_[a-f0-9]{16}$")
    raw_ref: str = Field(exclude=True, max_length=300)
    author: str = Field(default="", max_length=240)
    timestamp: str = Field(default="", max_length=120)
    text: str = Field(default="", max_length=6000)
    kind: Literal["text", "image", "audio", "video", "document"]
    provenance_ref: str
    epistemic_kind: Literal["reported_statement"] = "reported_statement"
    content_role: Literal["data"] = "data"
    outbound: bool = False


class CurrentChat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["FOUND", "NO_CHAT"]
    chat_id: str = ""
    title: str = ""
    participants: str = ""
    messages: tuple[VisibleMessage, ...] = ()
    oldest_loaded: str = ""
    newest_loaded: str = ""


class HistoryRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chat: CurrentChat
    messages_seen: int = Field(ge=0)
    scroll_iterations: int = Field(ge=0)
    history_exhausted: bool
    cap_reached: bool
    audit: tuple[NavigationAudit, ...]


class WhatsAppReadNavigationPolicy:
    DENIED_TERMS = frozenset({
        "send", "invia", "elimina", "delete", "modifica", "edit", "aggiungi", "add",
        "esci", "logout", "chiama", "call", "archivia", "archive", "blocca", "block",
        "reaction", "reazione", "inoltra", "forward", "submit", "pin", "silenzia", "mute",
    })

    @classmethod
    def classify_control(cls, label: str, *, allowlisted_operation: bool) -> str:
        folded = " ".join(label.casefold().split())
        if any(term in folded for term in cls.DENIED_TERMS):
            return "DENY"
        return "READ_NAVIGATION" if allowlisted_operation else "DENY"


class WhatsAppCdpNavigator:
    def __init__(
        self,
        client: CdpReadOnlySnapshotClient,
        *,
        wait_seconds: float = 0.6,
        max_scroll_iterations: int = 8,
    ) -> None:
        if not 0 <= wait_seconds <= 3:
            raise ValueError("whatsapp_wait_invalid")
        if not 1 <= max_scroll_iterations <= 64:
            raise ValueError("whatsapp_scroll_cap_invalid")
        self.client = client
        self.wait_seconds = wait_seconds
        self.max_scroll_iterations = max_scroll_iterations
        self.audit_events: list[NavigationAudit] = []

    @classmethod
    def from_endpoint(cls, endpoint: str, **kwargs) -> "WhatsAppCdpNavigator":
        client = CdpReadOnlySnapshotClient(
            endpoint, fixed_expressions=EXPRESSIONS, fixed_functions=FUNCTIONS,
        )
        return cls(client, **kwargs)

    def list_chats(self) -> tuple[ChatInventoryItem, ...]:
        raw = self._evaluate(_LIST_CHATS_SCRIPT, "whatsapp.list_chats")
        rows: list[ChatInventoryItem] = []
        for item in raw if isinstance(raw, list) else ():
            title = _clean(item.get("title"), 240) if isinstance(item, dict) else ""
            if not title:
                continue
            rows.append(ChatInventoryItem(
                chat_id=_chat_id(title), title=title,
                secondary=_clean(item.get("secondary"), 1000),
            ))
        # Preserve duplicate titles.  Title-derived identifiers are intentionally
        # conservative: a collision must surface as AMBIGUOUS_CHAT, never select
        # an arbitrary row.
        self._audit("list_chats", "READ", inspected=len(rows))
        return tuple(rows)

    def open_chat(self, query: str) -> dict[str, Any]:
        query = _clean(query, 200)
        if not query:
            return {"status": "INVALID", "candidates": []}
        raw = self._function(_OPEN_CHAT_FUNCTION, (query,), "whatsapp.open_chat")
        if isinstance(raw, dict) and raw.get("status") == "NAVIGATED":
            self._wait()
            title = _clean(raw.get("title"), 240)
            current = self.read_current_chat()
            verified = current.status == "FOUND" and current.title.casefold() == title.casefold()
            if not verified:
                self._wait()
                current = self.read_current_chat()
                verified = current.status == "FOUND" and current.title.casefold() == title.casefold()
            self._audit("open_chat", "READ_NAVIGATION", target=_chat_id(title))
            return {"status": "OPENED" if verified else "VERIFICATION_FAILED", "title": title, "candidates": []}
        candidates = [
            _clean(value, 240) for value in (raw.get("candidates") if isinstance(raw, dict) else ())
            if _clean(value, 240)
        ]
        return {"status": str(raw.get("status") if isinstance(raw, dict) else "UNAVAILABLE"), "candidates": candidates}

    def read_current_chat(self) -> CurrentChat:
        raw = self._evaluate(_READ_CURRENT_CHAT_SCRIPT, "whatsapp.read_visible_messages")
        if not isinstance(raw, dict) or raw.get("status") != "FOUND":
            return CurrentChat(status="NO_CHAT")
        title = _clean(raw.get("title"), 240)
        chat_id = _chat_id(title) if title else ""
        messages: list[VisibleMessage] = []
        for item in raw.get("messages") if isinstance(raw.get("messages"), list) else ():
            if not isinstance(item, dict):
                continue
            raw_id = _clean(item.get("raw_id"), 300)
            if not raw_id:
                continue
            author, timestamp = _parse_pre_plain(_clean(item.get("metadata"), 500))
            digest = hashlib.sha256(raw_id.encode()).hexdigest()[:16]
            messages.append(VisibleMessage(
                message_id=f"wa_msg_{digest}", raw_ref=raw_id,
                author=author, timestamp=timestamp,
                text=_clean(item.get("text"), 6000),
                kind=str(item.get("kind") or "text"),
                outbound=bool(item.get("outbound")),
                provenance_ref=f"whatsapp_web:{chat_id}:message:{digest}",
            ))
        self._audit("read_visible_messages", "READ", target=chat_id, inspected=len(messages))
        return CurrentChat(
            status="FOUND", chat_id=chat_id, title=title,
            participants=_clean(raw.get("participants"), 1000),
            messages=tuple(messages),
            oldest_loaded=messages[0].timestamp if messages else "",
            newest_loaded=messages[-1].timestamp if messages else "",
        )

    def search_global(
        self, query: str, *, keep_results_open: bool = False,
    ) -> tuple[dict[str, str], ...]:
        query = _clean(query, 200)
        if not query:
            raise ValueError("whatsapp_search_query_required")
        armed = self._function(_FOCUS_GLOBAL_SEARCH_FUNCTION, (), "whatsapp.search_global.focus")
        if not isinstance(armed, dict) or armed.get("status") != "ARMED":
            raise BrowserReadOnlyError("whatsapp_search_control_denied")
        self.client.insert_read_navigation_text(query)
        self._wait()
        raw = self._evaluate(_LIST_SEARCH_RESULTS_SCRIPT, "whatsapp.search_global.results")
        rows = tuple({
            "title": _clean(item.get("title"), 240),
            "excerpt": _clean(item.get("excerpt"), 1200),
        } for item in raw if isinstance(item, dict) and _clean(item.get("title"), 240)) if isinstance(raw, list) else ()
        wanted = _clean(query, 200).casefold()
        rows = tuple(
            item for item in rows
            if wanted in f"{item['title']} {item['excerpt']}".casefold()
        )
        if not keep_results_open:
            self._clear_global_search()
        self._audit("search_global", "READ_NAVIGATION", inspected=len(rows))
        return rows

    def locate_and_open_chat(self, query: str) -> dict[str, Any]:
        """Open an exact chat even when it is only present in global search."""

        direct = self.open_chat(query)
        if direct.get("status") != "NOT_FOUND":
            return direct
        rows = self.search_global(query, keep_results_open=True)
        exact = [
            item for item in rows
            if _clean(item.get("title"), 240).casefold() == _clean(query, 240).casefold()
        ]
        try:
            if len(exact) != 1:
                return {
                    "status": "AMBIGUOUS" if exact else "NOT_FOUND",
                    "candidates": [str(item.get("title") or "") for item in exact[:8]],
                }
            return self.open_chat(str(exact[0]["title"]))
        finally:
            self._clear_global_search()

    def scroll_history(self, *, max_iterations: int | None = None) -> HistoryRead:
        cap = self.max_scroll_iterations if max_iterations is None else max_iterations
        if not 1 <= cap <= self.max_scroll_iterations:
            raise ValueError("whatsapp_scroll_cap_invalid")
        current = self.read_current_chat()
        messages = {item.message_id: item for item in current.messages}
        unchanged = 0
        iterations = 0
        exhausted = False
        for _ in range(cap):
            raw = self._function(_SCROLL_HISTORY_FUNCTION, (), "whatsapp.scroll_history")
            if not isinstance(raw, dict) or raw.get("status") != "SCROLLED":
                exhausted = True
                break
            iterations += 1
            self._wait()
            updated = self.read_current_chat()
            before = len(messages)
            messages.update({item.message_id: item for item in updated.messages})
            unchanged = unchanged + 1 if len(messages) == before else 0
            current = updated
            if unchanged >= 2:
                exhausted = True
                break
        ordered = tuple(messages.values())
        current = current.model_copy(update={
            "messages": ordered,
            "oldest_loaded": ordered[0].timestamp if ordered else "",
            "newest_loaded": ordered[-1].timestamp if ordered else "",
        })
        cap_reached = iterations >= cap and not exhausted
        self._audit("scroll_history", "READ_NAVIGATION", target=current.chat_id, inspected=len(ordered))
        return HistoryRead(
            chat=current, messages_seen=len(ordered), scroll_iterations=iterations,
            history_exhausted=exhausted, cap_reached=cap_reached,
            audit=tuple(self.audit_events),
        )

    def read_chat_info(self) -> dict[str, Any]:
        opened = self._function(_OPEN_CHAT_INFO_FUNCTION, (), "whatsapp.open_chat_info")
        if not isinstance(opened, dict) or opened.get("status") != "NAVIGATED":
            return {"status": "UNAVAILABLE", "text": ""}
        self._wait()
        raw = self._evaluate(_READ_CHAT_INFO_SCRIPT, "whatsapp.read_chat_info")
        self.client.dispatch_read_navigation_key("Escape")
        self._wait()
        self._audit("read_chat_info", "READ_NAVIGATION")
        return {
            "status": str(raw.get("status") if isinstance(raw, dict) else "UNAVAILABLE"),
            "text": _clean(raw.get("text"), 12000) if isinstance(raw, dict) else "",
        }

    def open_media(self, raw_message_ref: str) -> str:
        raw = self._function(
            _OPEN_MEDIA_FUNCTION, (_clean(raw_message_ref, 300),), "whatsapp.open_media",
        )
        status = str(raw.get("status") if isinstance(raw, dict) else "UNAVAILABLE")
        if status == "NAVIGATED":
            self._wait()
            self._audit("open_media", "READ_NAVIGATION")
        return status

    def close_read_panel(self) -> None:
        self.client.dispatch_read_navigation_key("Escape")
        self._wait()
        self._audit("close_read_panel", "READ_NAVIGATION")

    def clear_search(self) -> bool:
        raw = self._function(
            _CLEAR_GLOBAL_SEARCH_FUNCTION, (), "whatsapp.search_global.clear",
        )
        self._wait()
        cleared = isinstance(raw, dict) and raw.get("status") in {"CLEARED", "ALREADY_CLEAR"}
        self._audit("clear_search", "READ_NAVIGATION")
        return cleared

    def _clear_global_search(self) -> None:
        if self.clear_search():
            self.client.dispatch_read_navigation_key("Escape")
            self._wait()

    def _evaluate(self, expression: str, operation: str) -> Any:
        return self.client.evaluate_fixed(
            expected_host="web.whatsapp.com", expression=expression, operation=operation,
        ).value

    def _function(self, function: str, arguments: tuple[Any, ...], operation: str) -> Any:
        return self.client.call_fixed_function(
            expected_host="web.whatsapp.com", function=function,
            arguments=arguments, operation=operation,
        ).value

    def _audit(
        self, operation: str, classification: str,
        *, target: str = "", inspected: int = 0, artifacts: int = 0,
    ) -> None:
        self.audit_events.append(NavigationAudit(
            operation=operation, classification=classification,
            target_ref=target, inspected_count=inspected,
            artifacts_acquired=artifacts,
        ))

    def _wait(self) -> None:
        if self.wait_seconds:
            time.sleep(self.wait_seconds)


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _chat_id(title: str) -> str:
    return "wa_chat_" + hashlib.sha256(title.casefold().encode()).hexdigest()[:16]


def _parse_pre_plain(value: str) -> tuple[str, str]:
    match = re.match(r"^\[([^\]]+)\]\s*([^:]{0,240}):?", value)
    if not match:
        return "", ""
    return _clean(match.group(2), 240), _clean(match.group(1), 120)


__all__ = [
    "ChatInventoryItem", "CurrentChat", "HistoryRead", "NavigationAudit",
    "VisibleMessage", "WhatsAppCdpNavigator", "WhatsAppReadNavigationPolicy",
]
