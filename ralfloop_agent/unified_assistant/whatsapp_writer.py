from __future__ import annotations

"""Fixed WhatsApp browser writer. Callable only behind approval-bound MCP tools."""

import os
from pathlib import Path
import time
from typing import Any
import unicodedata

from .browser_read_only import BrowserReadOnlyError, CdpReadOnlySnapshotClient
from .whatsapp_navigation import EXPRESSIONS, FUNCTIONS, WhatsAppCdpNavigator
from .whatsapp_web import WhatsAppScopeRegistry, WhatsAppWebReadOnly


_FOCUS_EMPTY_COMPOSER_FUNCTION = r"""
function() {
  const boxes = Array.from(document.querySelectorAll('#main footer [contenteditable="true"][role="textbox"]'));
  if (boxes.length !== 1) return {status: boxes.length ? 'AMBIGUOUS' : 'NOT_FOUND'};
  const box = boxes[0];
  if (String(box.innerText || '').trim()) return {status: 'DRAFT_PRESENT'};
  box.focus();
  return {status: 'ARMED'};
}
""".strip()


_CLICK_SEND_FUNCTION = r"""
function() {
  const footer = document.querySelector('#main footer');
  if (!footer) return {status: 'NOT_FOUND'};
  const candidates = Array.from(footer.querySelectorAll('button,[role="button"]')).filter((item) => {
    const label = String(item.getAttribute('aria-label') || item.getAttribute('title') || '').toLocaleLowerCase('it');
    return label === 'invia' || label === 'send' || Boolean(item.querySelector('[data-icon="send"]'));
  });
  if (candidates.length !== 1) return {status: candidates.length ? 'AMBIGUOUS' : 'NOT_FOUND'};
  candidates[0].click();
  return {status: 'SUBMITTED'};
}
""".strip()


_OPEN_REPLY_MENU_FUNCTION = r"""
function(messageId) {
  const main = document.querySelector('#main');
  const message = main && Array.from(main.querySelectorAll('[data-id]')).find((item) => item.getAttribute('data-id') === messageId);
  if (!message) return {status: 'REPLY_TARGET_UNAVAILABLE'};
  message.scrollIntoView({block: 'center'});
  message.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true, view: window}));
  return {status: 'MENU_OPEN'};
}
""".strip()


_CLICK_REPLY_ACTION_FUNCTION = r"""
function() {
  const menus = Array.from(document.querySelectorAll('[role="menu"]'));
  const controls = menus.flatMap((menu) => Array.from(menu.querySelectorAll('[role="menuitem"],li,button'))).filter((item) => {
    const text = String(item.innerText || item.getAttribute('aria-label') || '').trim().toLocaleLowerCase('it');
    return text === 'rispondi' || text === 'reply';
  });
  if (controls.length !== 1) return {status: controls.length ? 'AMBIGUOUS' : 'REPLY_TARGET_UNAVAILABLE'};
  controls[0].click();
  return {status: 'REPLY_ARMED'};
}
""".strip()


WRITE_FUNCTIONS = frozenset({
    _FOCUS_EMPTY_COMPOSER_FUNCTION, _CLICK_SEND_FUNCTION,
    _OPEN_REPLY_MENU_FUNCTION, _CLICK_REPLY_ACTION_FUNCTION,
})


class WhatsAppBrowserWriter:
    def __init__(self, provider: WhatsAppWebReadOnly, *, wait_seconds: float = 0.8) -> None:
        self.provider = provider
        self.navigator = provider.navigator
        self.client = self.navigator.client
        self.wait_seconds = wait_seconds

    @classmethod
    def from_environment(cls) -> "WhatsAppBrowserWriter":
        root = Path(__file__).resolve().parents[2]
        registry = WhatsAppScopeRegistry.load(os.getenv(
            "RALFLOOP_WHATSAPP_SCOPE_REGISTRY",
            str(root / "config" / "whatsapp_memory_scopes_v1.json"),
        ))
        endpoint = os.getenv("RALFLOOP_WHATSAPP_CDP_ENDPOINT", "http://127.0.0.1:9236")
        client = CdpReadOnlySnapshotClient(
            endpoint, fixed_expressions=EXPRESSIONS,
            fixed_functions=FUNCTIONS | WRITE_FUNCTIONS,
        )
        navigator = WhatsAppCdpNavigator(client)
        return cls(WhatsAppWebReadOnly(navigator, registry))

    def send_message(
        self, *, chat_id: str, chat_title: str, body: str, execution_id: str,
    ) -> dict[str, Any]:
        return self._send(
            chat_id=chat_id, chat_title=chat_title, body=body,
            execution_id=execution_id, reply_message_id="",
        )

    def reply_message(
        self, *, chat_id: str, chat_title: str, message_id: str,
        body: str, execution_id: str,
    ) -> dict[str, Any]:
        return self._send(
            chat_id=chat_id, chat_title=chat_title, body=body, execution_id=execution_id,
            reply_message_id=message_id,
        )

    def _send(
        self, *, chat_id: str, chat_title: str, body: str,
        execution_id: str, reply_message_id: str,
    ) -> dict[str, Any]:
        body = str(body)
        if (
            not chat_id.startswith("wa_chat_") or not chat_title
            or body != body.strip() or not 1 <= len(body) <= 4000
        ):
            return self._error("POLICY_DENIED", execution_id)
        chats = [item for item in self.provider.list_chats() if item.chat_id == chat_id]
        if len(chats) != 1:
            return self._error("CHAT_NOT_FOUND" if not chats else "AMBIGUOUS_CHAT", execution_id)
        if chats[0].title != chat_title:
            return self._error("CHAT_IDENTITY_CHANGED", execution_id)
        if self.provider.open_chat(chat_id).get("status") != "OPENED":
            return self._error("CHAT_NOT_FOUND", execution_id)
        before = self.provider.read_messages(chat_id, limit=100)
        before_ids = {item.message_id for item in before.messages}
        if reply_message_id:
            match = next((item for item in before.messages if item.message_id == reply_message_id), None)
            if match is None:
                return self._error("REPLY_TARGET_UNAVAILABLE", execution_id)
            opened = self._function(_OPEN_REPLY_MENU_FUNCTION, (match.raw_ref,))
            if opened.get("status") != "MENU_OPEN":
                return self._error("REPLY_TARGET_UNAVAILABLE", execution_id)
            self._wait()
            armed = self._function(_CLICK_REPLY_ACTION_FUNCTION, ())
            if armed.get("status") != "REPLY_ARMED":
                return self._error("REPLY_TARGET_UNAVAILABLE", execution_id)
        composer = self._function(_FOCUS_EMPTY_COMPOSER_FUNCTION, ())
        if composer.get("status") != "ARMED":
            return self._error(str(composer.get("status") or "SEND_FAILED"), execution_id)
        for start in range(0, len(body), 200):
            self.client.insert_read_navigation_text(body[start:start + 200])
        submitted = self._function(_CLICK_SEND_FUNCTION, ())
        if submitted.get("status") != "SUBMITTED":
            return self._error("SEND_FAILED", execution_id)
        self._wait()
        after = self.provider.read_messages(chat_id, limit=100)
        wanted = _canonical_observed_text(body)
        observed = next(
            (
                item for item in reversed(after.messages)
                if item.message_id not in before_ids and item.outbound
                and _canonical_observed_text(item.text) == wanted
            ),
            None,
        )
        if observed is None:
            return {
                **self._error("SEND_UNVERIFIED", execution_id),
                "exact_text_observed": False,
            }
        return {
            "ok": True, "status": "executed", "operation": (
                "reply_message" if reply_message_id else "send_message"
            ),
            "execution_id": execution_id, "chat_id": chat_id,
            "message_id": observed.message_id, "verified": True,
            "exact_text_observed": True, "side_effects": 1,
        }

    def _function(self, function: str, arguments: tuple[Any, ...]) -> dict[str, Any]:
        try:
            value = self.client.call_fixed_function(
                expected_host="web.whatsapp.com", function=function,
                arguments=arguments, operation="whatsapp.approved_write",
            ).value
        except BrowserReadOnlyError:
            return {"status": "SOURCE_UNAVAILABLE"}
        return value if isinstance(value, dict) else {"status": "SEND_FAILED"}

    def _wait(self) -> None:
        if self.wait_seconds:
            time.sleep(self.wait_seconds)

    @staticmethod
    def _error(status: str, execution_id: str) -> dict[str, Any]:
        return {
            "ok": False, "status": status, "execution_id": execution_id,
            "verified": False, "side_effects": 0,
        }


def _canonical_observed_text(value: str) -> str:
    """Canonicalize transport newlines/Unicode only; preserve case and spacing."""

    return unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")


__all__ = ["WhatsAppBrowserWriter", "WRITE_FUNCTIONS"]
