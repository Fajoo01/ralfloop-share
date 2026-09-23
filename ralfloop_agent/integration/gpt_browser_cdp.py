from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import websocket

DEFAULT_ENDPOINT = os.getenv("BOTTAZZI_GPT_CDP_ENDPOINT", "http://127.0.0.1:9238")
CHATGPT_ORIGIN = "https://chatgpt.com/"


class CdpError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserTarget:
    target_id: str
    target_type: str
    url: str
    title: str
    websocket_url: str | None = None

    @property
    def is_chatgpt(self) -> bool:
        try:
            host = urllib.parse.urlparse(self.url).hostname or ""
        except ValueError:
            return False
        return host == "chatgpt.com" or host.endswith(".chatgpt.com")


class ChromeCdp:
    def __init__(self, endpoint: str = DEFAULT_ENDPOINT, *, timeout_s: float = 5.0) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.timeout_s = timeout_s
        self._next_id = 1

    def health(self) -> dict[str, Any]:
        return self._http_json("/json/version")

    def targets(self) -> list[BrowserTarget]:
        raw = self._http_json("/json/list")
        if not isinstance(raw, list):
            raise CdpError("invalid_target_list")
        out: list[BrowserTarget] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            target_id = str(item.get("id") or "")
            if not target_id:
                continue
            out.append(
                BrowserTarget(
                    target_id=target_id,
                    target_type=str(item.get("type") or ""),
                    url=str(item.get("url") or ""),
                    title=str(item.get("title") or ""),
                    websocket_url=str(item.get("webSocketDebuggerUrl")) if item.get("webSocketDebuggerUrl") else None,
                )
            )
        return out

    def chatgpt_ui_state(self, target_id: str | None = None) -> dict[str, Any]:
        pages = [target for target in self.targets() if target.target_type == "page" and target.is_chatgpt]
        if target_id is not None:
            pages = [target for target in pages if target.target_id == target_id]
        if not pages:
            return {"ready": False, "reason": "chatgpt_tab_not_found", "user_turns": 0, "assistant_turns": 0}
        target = pages[-1]
        if not target.websocket_url:
            raise CdpError("chatgpt_target_missing_websocket")
        expression = r"""(() => {
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled;
          };
          const selectors = ['#prompt-textarea', 'textarea', '[contenteditable="true"]'];
          let composer = null;
          for (const selector of selectors) {
            composer = Array.from(document.querySelectorAll(selector)).find(visible) || null;
            if (composer) break;
          }
          const loginControls = Array.from(document.querySelectorAll('button,a'))
            .filter(visible)
            .filter((el) => /(?:log in|sign in|accedi|registrati|sign up)/i.test((el.innerText || '').trim()));
          const authenticatedHint = loginControls.length === 0;
          return JSON.stringify({
            ready: Boolean(composer) && authenticatedHint,
            composer_ready: Boolean(composer),
            authenticated_hint: authenticatedHint,
            login_controls: loginControls.length,
            title: document.title || '',
            url: location.href,
            user_turns: document.querySelectorAll('[data-message-author-role="user"]').length,
            assistant_turns: document.querySelectorAll('[data-message-author-role="assistant"]').length,
            page_age_minutes: Math.max(0, Math.floor(performance.now() / 60000)),
            interaction_required: !authenticatedHint || !composer || /ci siamo quasi/i.test(document.title || ''),
            composer_kind: composer ? (composer.id || composer.tagName || '').toLowerCase() : null,
          });
        })()"""
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("chatgpt_ui_state_invalid") from exc
        if not isinstance(state, dict):
            raise CdpError("chatgpt_ui_state_invalid")
        state["target_id"] = target.target_id
        return state

    def inject_prompt(
        self,
        prompt: str,
        *,
        target_id: str | None = None,
        submit: bool = False,
        wait_timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise CdpError("empty_prompt")
        deadline = time.monotonic() + wait_timeout_s
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = self.chatgpt_ui_state(target_id)
            if state.get("ready"):
                break
            time.sleep(0.25)
        if not state.get("ready"):
            reason = "interaction_required" if state.get("interaction_required") else "composer_not_ready"
            raise CdpError(f"chatgpt_not_ready:{reason}")

        target = self._wait_target(str(state["target_id"]))
        baseline_user_turns = int(state.get("user_turns") or 0)
        if not target.websocket_url:
            raise CdpError("chatgpt_target_missing_websocket")
        prompt_json = json.dumps(prompt, ensure_ascii=False)
        expression = f"""(() => {{
          const visible = (el) => {{
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled;
          }};
          const selectors = ['#prompt-textarea', 'textarea', '[contenteditable="true"]'];
          let el = null;
          for (const selector of selectors) {{
            el = Array.from(document.querySelectorAll(selector)).find(visible) || null;
            if (el) break;
          }}
          if (!el) return JSON.stringify({{ok:false, reason:'composer_not_found'}});
          const text = {prompt_json};
          el.focus();
          if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {{
            const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            setter.call(el, text);
            el.dispatchEvent(new Event('input', {{bubbles:true}}));
          }} else {{
            el.textContent = '';
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(el);
            range.collapse(true);
            selection.removeAllRanges();
            selection.addRange(range);
            document.execCommand('insertText', false, text);
            el.dispatchEvent(new InputEvent('input', {{bubbles:true, inputType:'insertText', data:text}}));
          }}
          return JSON.stringify({{ok:true}});
        }})()"""
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
        )
        raw = (result.get("result") or {}).get("value")
        try:
            injected = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("prompt_injection_invalid") from exc
        if not isinstance(injected, dict) or not injected.get("ok"):
            reason = injected.get("reason", "unknown") if isinstance(injected, dict) else "unknown"
            raise CdpError(f"prompt_injection_failed:{reason}")
        submit_confirmed = False
        if submit:
            common = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
            self._page_call(target.websocket_url, "Input.dispatchKeyEvent", {"type": "keyDown", **common})
            self._page_call(target.websocket_url, "Input.dispatchKeyEvent", {"type": "keyUp", **common})
            confirm_deadline = time.monotonic() + 10.0
            while time.monotonic() < confirm_deadline:
                current_target = self._wait_target(target.target_id)
                if not current_target.is_chatgpt:
                    raise CdpError("submit_interaction_required")
                current_state = self.chatgpt_ui_state(target.target_id)
                if int(current_state.get("user_turns") or 0) > baseline_user_turns:
                    submit_confirmed = True
                    break
                time.sleep(0.25)
            if not submit_confirmed:
                raise CdpError("submit_not_confirmed")
        return {"target_id": target.target_id, "injected": True, "submitted": submit_confirmed, "submit_confirmed": submit_confirmed}

    def handoff_to_new_chat(self, prompt: str, *, submit: bool = True) -> dict[str, Any]:
        previous = [target for target in self.targets() if target.target_type == "page" and target.is_chatgpt]
        target_id = self.create_target("about:blank")
        target = self._wait_target(target_id)
        if not target.websocket_url:
            raise CdpError("new_target_missing_websocket")
        self._page_call(target.websocket_url, "Network.enable")
        self._page_call(target.websocket_url, "Network.clearBrowserCache")
        self._page_call(target.websocket_url, "Page.enable")
        self._page_call(target.websocket_url, "Page.navigate", {"url": CHATGPT_ORIGIN})
        try:
            injected = self.inject_prompt(prompt, target_id=target_id, submit=submit)
        except Exception:
            self.close_target(target_id)
            raise
        closed: list[str] = []
        for old in previous:
            if old.target_id != target_id:
                self.close_target(old.target_id)
                closed.append(old.target_id)
        return {
            **injected,
            "new_target_id": target_id,
            "closed_target_ids": closed,
            "server_chat_deleted": False,
            "cache_cleared": True,
        }

    def rotate_chatgpt_tab(self, *, close_old: bool = True) -> dict[str, Any]:
        previous = [target for target in self.targets() if target.target_type == "page" and target.is_chatgpt]
        target_id = self.create_target("about:blank")
        target = self._wait_target(target_id)
        if not target.websocket_url:
            raise CdpError("new_target_missing_websocket")
        self._page_call(target.websocket_url, "Network.enable")
        self._page_call(target.websocket_url, "Network.clearBrowserCache")
        if close_old:
            for old in previous:
                if old.target_id != target_id:
                    self.close_target(old.target_id)
        self._page_call(target.websocket_url, "Page.enable")
        self._page_call(target.websocket_url, "Page.navigate", {"url": CHATGPT_ORIGIN})
        return {
            "new_target_id": target_id,
            "closed_target_ids": [item.target_id for item in previous if close_old and item.target_id != target_id],
            "server_chat_deleted": False,
            "cache_cleared": True,
        }

    def create_target(self, url: str) -> str:
        response = self._browser_call("Target.createTarget", {"url": url})
        target_id = str(response.get("targetId") or "")
        if not target_id:
            raise CdpError("target_create_failed")
        return target_id

    def close_target(self, target_id: str) -> None:
        response = self._browser_call("Target.closeTarget", {"targetId": target_id})
        if response.get("success") is False:
            raise CdpError(f"target_close_failed:{target_id}")

    def clear_cache(self) -> int:
        count = 0
        for target in self.targets():
            if target.target_type != "page" or not target.websocket_url:
                continue
            self._page_call(target.websocket_url, "Network.enable")
            self._page_call(target.websocket_url, "Network.clearBrowserCache")
            count += 1
        return count

    def performance_metrics(self, target_id: str) -> dict[str, float]:
        target = self._wait_target(target_id)
        if not target.websocket_url:
            raise CdpError("target_missing_websocket")
        self._page_call(target.websocket_url, "Performance.enable")
        result = self._page_call(target.websocket_url, "Performance.getMetrics")
        metrics: dict[str, float] = {}
        for item in result.get("metrics") or []:
            if isinstance(item, dict) and "name" in item and "value" in item:
                try:
                    metrics[str(item["name"])] = float(item["value"])
                except (TypeError, ValueError):
                    continue
        return metrics

    def _wait_target(self, target_id: str, *, attempts: int = 20) -> BrowserTarget:
        for _ in range(attempts):
            for target in self.targets():
                if target.target_id == target_id:
                    return target
            time.sleep(0.1)
        raise CdpError(f"target_not_found:{target_id}")

    def _browser_call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        version = self.health()
        websocket_url = str(version.get("webSocketDebuggerUrl") or "")
        if not websocket_url:
            raise CdpError("browser_websocket_missing")
        return self._rpc(websocket_url, method, params)

    def _page_call(self, websocket_url: str, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._rpc(websocket_url, method, params)

    def _rpc(self, websocket_url: str, method: str, params: dict[str, Any] | None) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        ws = websocket.create_connection(
            websocket_url,
            timeout=self.timeout_s,
            suppress_origin=True,
        )
        try:
            ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            deadline = time.monotonic() + self.timeout_s
            while time.monotonic() < deadline:
                raw = ws.recv()
                message = json.loads(raw)
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise CdpError(f"cdp_error:{method}:{message['error']}")
                result = message.get("result")
                return result if isinstance(result, dict) else {}
            raise CdpError(f"cdp_timeout:{method}")
        finally:
            ws.close()

    def _http_json(self, path: str) -> Any:
        request = urllib.request.Request(self.endpoint + path, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise CdpError(f"cdp_unavailable:{self.endpoint}") from exc
