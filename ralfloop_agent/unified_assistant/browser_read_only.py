from __future__ import annotations

import json
import base64
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any, Iterable
from urllib.parse import urlparse
from urllib.request import urlopen


class BrowserReadOnlyError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserPage:
    page_id: str
    title: str
    url: str
    websocket_url: str


@dataclass(frozen=True)
class AccessibilitySnapshot:
    page: BrowserPage
    nodes: tuple[dict[str, Any], ...]
    operations: tuple[str, ...] = ("cdp.list", "cdp.accessibility_snapshot")


@dataclass(frozen=True)
class CapturedMedia:
    kind: str
    mime_type: str
    content: bytes
    description: str


@dataclass(frozen=True)
class FixedScriptResult:
    value: Any
    operation: str


_VISIBLE_MEDIA_SCRIPT = r"""
(async () => {
  const root = document.querySelector('[data-testid="media-viewer-modal"]') || document.querySelector('#main');
  if (!root) return [];
  const elements = Array.from(root.querySelectorAll('img,audio,video,a[download]')).filter((element) => {
    const box = element.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  }).slice(0, 6);
  const output = [];
  for (const element of elements) {
    const source = element.currentSrc || element.src || element.href || '';
    if (!(source.startsWith('blob:') || source.startsWith('data:'))) continue;
    try {
      const blob = await (await fetch(source, {method: 'GET'})).blob();
      if (blob.size > 8388608) {
        output.push({kind: element.tagName.toLowerCase(), mime: blob.type, size: blob.size, status: 'too_large'});
        continue;
      }
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
      output.push({
        kind: element.hasAttribute('download') ? 'document' : element.tagName.toLowerCase(), mime: blob.type,
        size: blob.size, status: 'captured', data,
        description: String(element.alt || element.getAttribute('aria-label') || '').slice(0, 240)
      });
    } catch (_) {
      output.push({kind: element.tagName.toLowerCase(), mime: '', size: 0, status: 'unavailable'});
    }
  }
  return output;
})()
""".strip()

_MESSAGE_MEDIA_FUNCTION = r"""
async function(messageId) {
  const main = document.querySelector('#main');
  const message = main && Array.from(main.querySelectorAll('[data-id]')).find(
    (item) => item.getAttribute('data-id') === messageId
  );
  if (!message) return [];
  const elements = Array.from(message.querySelectorAll('img,audio,video,a[download]')).filter((element) => {
    const box = element.getBoundingClientRect();
    return box.width > 40 && box.height > 40;
  }).slice(0, 6);
  const output = [];
  for (const element of elements) {
    const source = element.currentSrc || element.src || element.href || '';
    if (!(source.startsWith('blob:') || source.startsWith('data:'))) continue;
    try {
      const blob = await (await fetch(source, {method: 'GET'})).blob();
      if (blob.size > 8388608) {
        output.push({kind: element.tagName.toLowerCase(), mime: blob.type, size: blob.size, status: 'too_large'});
        continue;
      }
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = reject;
        reader.readAsDataURL(blob);
      });
      output.push({
        kind: element.hasAttribute('download') ? 'document' : element.tagName.toLowerCase(),
        mime: blob.type, size: blob.size, status: 'captured', data,
        description: String(element.alt || element.getAttribute('aria-label') || '').slice(0, 240)
      });
    } catch (_) {
      output.push({kind: element.tagName.toLowerCase(), mime: '', size: 0, status: 'unavailable'});
    }
  }
  return output;
}
""".strip()


class CdpReadOnlySnapshotClient:
    """Fixed CDP surface: inventory and accessibility snapshot only."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_s: float = 5.0,
        fixed_expressions: Iterable[str] = (),
        fixed_functions: Iterable[str] = (),
    ) -> None:
        parsed = urlparse(endpoint.rstrip("/"))
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("cdp_endpoint_invalid")
        try:
            if not ip_address(parsed.hostname).is_loopback:
                raise ValueError("cdp_endpoint_must_be_loopback")
        except ValueError as exc:
            if str(exc) == "cdp_endpoint_must_be_loopback":
                raise
            raise ValueError("cdp_endpoint_must_be_loopback") from exc
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s
        self._fixed_expressions = frozenset(fixed_expressions)
        self._fixed_functions = frozenset((*fixed_functions, _MESSAGE_MEDIA_FUNCTION))

    def pages(self) -> tuple[BrowserPage, ...]:
        raw = self._get_json("/json/list")
        if not isinstance(raw, list):
            raise BrowserReadOnlyError("cdp_page_inventory_invalid")
        rows: list[BrowserPage] = []
        for item in raw:
            if not isinstance(item, dict) or item.get("type") != "page":
                continue
            websocket_url = str(item.get("webSocketDebuggerUrl") or "")
            if not websocket_url.startswith("ws://"):
                continue
            rows.append(BrowserPage(
                page_id=str(item.get("id") or ""),
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                websocket_url=websocket_url,
            ))
        return tuple(rows)

    def snapshot(self, *, expected_host: str, expected_path_prefix: str = "/") -> AccessibilitySnapshot:
        page = self._page(expected_host, expected_path_prefix)
        response = self._call(page, "Accessibility.getFullAXTree", {})
        nodes = response.get("result", {}).get("nodes")
        if not isinstance(nodes, list):
            raise BrowserReadOnlyError("cdp_accessibility_snapshot_invalid")
        return AccessibilitySnapshot(page=page, nodes=tuple(item for item in nodes if isinstance(item, dict)))

    def visible_whatsapp_media(self) -> tuple[CapturedMedia, ...]:
        """Fixed script; current #main only; blob/data GET only; bounded at 6 x 8 MiB."""

        page = self._page("web.whatsapp.com", "/")
        response = self._call(page, "Runtime.evaluate", {
            "expression": _VISIBLE_MEDIA_SCRIPT,
            "awaitPromise": True,
            "returnByValue": True,
        })
        value = response.get("result", {}).get("result", {}).get("value")
        if not isinstance(value, list):
            raise BrowserReadOnlyError("cdp_visible_media_invalid")
        return self._decode_captured_media(value)

    def whatsapp_message_media(self, raw_message_ref: str) -> tuple[CapturedMedia, ...]:
        """Acquire only blobs/data URLs contained by one exact WhatsApp message."""

        if not raw_message_ref or len(raw_message_ref) > 300:
            raise BrowserReadOnlyError("whatsapp_message_ref_invalid")
        value = self.call_fixed_function(
            expected_host="web.whatsapp.com", function=_MESSAGE_MEDIA_FUNCTION,
            arguments=(raw_message_ref,), operation="whatsapp.message_media.read",
        ).value
        if not isinstance(value, list):
            raise BrowserReadOnlyError("cdp_message_media_invalid")
        return self._decode_captured_media(value)

    @staticmethod
    def _decode_captured_media(value: list[Any]) -> tuple[CapturedMedia, ...]:
        rows: list[CapturedMedia] = []
        for item in value[:6]:
            if not isinstance(item, dict) or item.get("status") != "captured":
                continue
            kind = str(item.get("kind") or "")
            mime = str(item.get("mime") or "")
            if kind == "img" and not mime.startswith("image/"):
                continue
            if kind == "audio" and not mime.startswith("audio/"):
                continue
            if kind == "video" and not mime.startswith("video/"):
                continue
            if kind == "document" and not mime:
                mime = "application/octet-stream"
            raw = str(item.get("data") or "")
            if ";base64," not in raw:
                continue
            try:
                content = base64.b64decode(raw.split(";base64,", 1)[1], validate=True)
            except ValueError:
                continue
            if not content or len(content) > 8 * 1024 * 1024:
                continue
            if kind == "img" and len(content) < 2048:
                continue
            rows.append(CapturedMedia(
                kind={"img": "image", "audio": "audio", "video": "video"}.get(kind, "document"),
                mime_type=mime[:120], content=content,
                description=" ".join(str(item.get("description") or "").split())[:240],
            ))
        return tuple(rows)

    def evaluate_fixed(
        self,
        *,
        expected_host: str,
        expression: str,
        operation: str,
        await_promise: bool = False,
    ) -> FixedScriptResult:
        if expression not in self._fixed_expressions:
            raise BrowserReadOnlyError("cdp_expression_denied")
        page = self._page(expected_host, "/")
        response = self._call(page, "Runtime.evaluate", {
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        })
        value = response.get("result", {}).get("result", {}).get("value")
        return FixedScriptResult(value=value, operation=operation)

    def call_fixed_function(
        self,
        *,
        expected_host: str,
        function: str,
        arguments: tuple[Any, ...],
        operation: str,
    ) -> FixedScriptResult:
        if function not in self._fixed_functions:
            raise BrowserReadOnlyError("cdp_function_denied")
        page = self._page(expected_host, "/")
        document, response = self._call_sequence(page, (
            ("Runtime.evaluate", {"expression": "document", "returnByValue": False}),
            ("Runtime.callFunctionOn", {
                "functionDeclaration": function,
                "arguments": [{"value": value} for value in arguments],
                "awaitPromise": True,
                "returnByValue": True,
            }),
        ), inject_object_id=True)
        object_id = document.get("result", {}).get("result", {}).get("objectId")
        if not object_id:
            raise BrowserReadOnlyError("cdp_document_unavailable")
        value = response.get("result", {}).get("result", {}).get("value")
        return FixedScriptResult(value=value, operation=operation)

    def insert_read_navigation_text(self, text: str) -> None:
        if not 1 <= len(text) <= 200:
            raise BrowserReadOnlyError("read_navigation_text_invalid")
        page = self._page("web.whatsapp.com", "/")
        self._call(page, "Input.insertText", {"text": text})

    def dispatch_read_navigation_key(self, key: str) -> None:
        if key not in {"Backspace", "Escape"}:
            raise BrowserReadOnlyError("read_navigation_key_denied")
        page = self._page("web.whatsapp.com", "/")
        params = {"type": "keyDown", "key": key, "code": key}
        self._call(page, "Input.dispatchKeyEvent", params)
        self._call(page, "Input.dispatchKeyEvent", {**params, "type": "keyUp"})

    def _page(self, expected_host: str, expected_path_prefix: str) -> BrowserPage:
        matches: list[BrowserPage] = []
        for page in self.pages():
            parsed = urlparse(page.url)
            if (
                parsed.scheme == "https"
                and (parsed.hostname or "").casefold() == expected_host.casefold()
                and parsed.path.startswith(expected_path_prefix)
            ):
                matches.append(page)
        if len(matches) != 1:
            raise BrowserReadOnlyError("browser_page_unresolved" if not matches else "browser_page_ambiguous")
        return matches[0]

    def _call(self, page: BrowserPage, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self._call_sequence(page, ((method, params),))[0]

    def _call_sequence(
        self,
        page: BrowserPage,
        calls: tuple[tuple[str, dict[str, Any]], ...],
        *,
        inject_object_id: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        for method, params in calls:
            self._validate_call(method, params)
        responses: list[dict[str, Any]] = []
        try:
            import websocket

            ws = websocket.create_connection(page.websocket_url, timeout=self.timeout_s)
            try:
                for index, (method, original) in enumerate(calls, start=1):
                    params = dict(original)
                    if inject_object_id and index == 2:
                        object_id = responses[0].get("result", {}).get("result", {}).get("objectId")
                        if not object_id:
                            raise BrowserReadOnlyError("cdp_document_unavailable")
                        params["objectId"] = object_id
                    ws.send(json.dumps({"id": index, "method": method, "params": params}))
                    while True:
                        response = json.loads(ws.recv())
                        if response.get("id") == index:
                            break
                    if response.get("error"):
                        raise BrowserReadOnlyError("cdp_read_operation_failed")
                    responses.append(response)
            finally:
                ws.close()
        except BrowserReadOnlyError:
            raise
        except Exception as exc:
            raise BrowserReadOnlyError("cdp_accessibility_snapshot_failed") from exc
        return tuple(responses)

    def _validate_call(self, method: str, params: dict[str, Any]) -> None:
        if method not in {
            "Accessibility.getFullAXTree", "Runtime.evaluate", "Runtime.callFunctionOn",
            "Input.insertText", "Input.dispatchKeyEvent",
        }:
            raise BrowserReadOnlyError("cdp_method_denied")
        if method == "Runtime.evaluate":
            expression = params.get("expression")
            if expression not in {*self._fixed_expressions, _VISIBLE_MEDIA_SCRIPT, "document"}:
                raise BrowserReadOnlyError("cdp_expression_denied")
        if method == "Runtime.callFunctionOn" and params.get("functionDeclaration") not in self._fixed_functions:
            raise BrowserReadOnlyError("cdp_function_denied")

    def _get_json(self, path: str) -> Any:
        try:
            with urlopen(self.endpoint + path, timeout=self.timeout_s) as response:
                return json.load(response)
        except Exception as exc:
            raise BrowserReadOnlyError("cdp_inventory_unavailable") from exc


def ax_name(node: dict[str, Any]) -> str:
    return " ".join(str((node.get("name") or {}).get("value") or "").split())


def ax_role(node: dict[str, Any]) -> str:
    return str((node.get("role") or {}).get("value") or "")


__all__ = [
    "AccessibilitySnapshot",
    "BrowserPage",
    "BrowserReadOnlyError",
    "CapturedMedia",
    "CdpReadOnlySnapshotClient",
    "FixedScriptResult",
    "ax_name",
    "ax_role",
]
