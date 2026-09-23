from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

import websocket

DEFAULT_ENDPOINT = os.getenv("BOTTAZZI_GPT_CDP_ENDPOINT", "http://127.0.0.1:9238")
CHATGPT_ORIGIN = "https://chatgpt.com/"


def _canonical_chatgpt_conversation_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
        return None
    match = re.fullmatch(r"(?:/g/[^/]+)?/c/([A-Za-z0-9-]+)", parsed.path.rstrip("/"))
    if not match:
        return None
    return f"https://chatgpt.com/c/{match.group(1)}"


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
            composer = Array.from(document.querySelectorAll(selector)).find(el => visible(el) && el.id !== 'bottazzi-human-composer') || null;
            if (composer) break;
          }
          const loginControls = Array.from(document.querySelectorAll('button,a'))
            .filter(visible)
            .filter((el) => /(?:log in|sign in|accedi|registrati|sign up)/i.test((el.innerText || '').trim()));
          const authenticatedHint = loginControls.length === 0;
          const pageAgeMs = Math.max(0, Math.floor(performance.now()));
          const pageSettled = document.readyState === 'complete' && pageAgeMs >= 3000;
          const telemetryKey = '__bottazziGptTelemetryV2';
          const observerKey = '__bottazziGptTelemetryObserverV2';
          const responseErrorRe = /(?:something went wrong|error generating|network error|there was an error|si è verificato un errore|errore (?:di rete|durante|nella|nel)|riprova|try again)/i;
          const temporaryAccessLimitRe = /(?:temporarily limited access to (?:your )?conversations|temporaneamente (?:limitato )?l['’]?accesso alle conversazioni|attendere qualche minuto prima di riprovare|wait a few minutes before trying again)/i;
          const pageText = String(document.body ? document.body.innerText || '' : '');
          const temporaryAccessLimited = temporaryAccessLimitRe.test(pageText);
          const sampleTelemetry = () => {
            const nowMs = performance.now();
            const userTurns = document.querySelectorAll('[data-message-author-role="user"]').length;
            const assistantNodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
            const assistantTurns = assistantNodes.length;
            const stopSelectors = [
              'button[data-testid="stop-button"]',
              'button[aria-label*="Stop"]',
              'button[aria-label*="stop"]',
              'button[aria-label*="Interrompi"]',
              'button[aria-label*="interrompi"]',
            ];
            const responseInProgress = stopSelectors.some((selector) => Array.from(document.querySelectorAll(selector)).some(visible));
            const alertError = Array.from(document.querySelectorAll('[role="alert"], [data-testid*="error"]'))
              .filter(visible)
              .some((el) => responseErrorRe.test((el.innerText || el.textContent || '').trim()));
            const lastAssistant = assistantNodes.length ? assistantNodes[assistantNodes.length - 1] : null;
            const lastAssistantError = Boolean(lastAssistant && responseErrorRe.test((lastAssistant.innerText || lastAssistant.textContent || '').trim()));
            const assistantText = lastAssistant ? (lastAssistant.innerText || lastAssistant.textContent || '') : '';
            let assistantHash = 2166136261;
            const hashStep = Math.max(1, Math.floor(assistantText.length / 128));
            for (let i = 0; i < assistantText.length; i += hashStep) {
              assistantHash ^= assistantText.charCodeAt(i);
              assistantHash = Math.imul(assistantHash, 16777619);
            }
            const assistantProgressSignature = [assistantTurns, assistantText.length, lastAssistant ? lastAssistant.querySelectorAll('*').length : 0, assistantHash >>> 0].join(':');
            const toolIcons = Array.from(document.querySelectorAll('[data-testid="cot-v5-native-tool-icon"]'));
            let toolHash = 2166136261;
            for (const icon of toolIcons.slice(-32)) {
              const row = icon.closest('.group');
              const label = row ? ((row.querySelector('button[aria-label]') || {}).getAttribute?.('aria-label') || '') : '';
              for (let i = 0; i < label.length; i += Math.max(1, Math.floor(label.length / 32))) {
                toolHash ^= label.charCodeAt(i);
                toolHash = Math.imul(toolHash, 16777619);
              }
            }
            const progressSignature = [assistantProgressSignature, toolIcons.length, toolHash >>> 0].join(':');
            let telemetry = window[telemetryKey];
            const resetTelemetry = !telemetry || typeof telemetry !== 'object' || telemetry.version !== 2 || telemetry.url !== location.href || userTurns < Number(telemetry.last_user_turns || 0) || assistantTurns < Number(telemetry.last_assistant_turns || 0);
            if (resetTelemetry) {
              telemetry = {
                version: 2,
                url: location.href,
                last_user_turns: userTurns,
                last_assistant_turns: assistantTurns,
                pending_started_ms: responseInProgress ? nowMs : null,
                pending_assistant_turns_start: responseInProgress ? Math.max(0, assistantTurns - 1) : assistantTurns,
                generation_seen: responseInProgress,
                last_progress_ms: nowMs,
                last_progress_signature: progressSignature,
                last_response_latency_ms: 0,
                consecutive_errors: 0,
                last_error_user_turns: responseInProgress ? Math.max(0, userTurns - 1) : userTurns,
              };
              window[telemetryKey] = telemetry;
            } else {
              if (userTurns > Number(telemetry.last_user_turns || 0)) {
                telemetry.pending_started_ms = nowMs;
                telemetry.pending_assistant_turns_start = assistantTurns;
                telemetry.generation_seen = responseInProgress;
                telemetry.last_progress_ms = nowMs;
                telemetry.last_progress_signature = progressSignature;
              }
              if (telemetry.pending_started_ms !== null && responseInProgress) {
                telemetry.generation_seen = true;
              }
              if (telemetry.pending_started_ms !== null && progressSignature !== telemetry.last_progress_signature) {
                telemetry.last_progress_ms = nowMs;
              }
              telemetry.last_progress_signature = progressSignature;
              const assistantAdvanced = assistantTurns > Number(telemetry.pending_assistant_turns_start || 0);
              const currentError = alertError || (lastAssistantError && assistantAdvanced);
              if (currentError && userTurns > Number(telemetry.last_error_user_turns || 0)) {
                telemetry.consecutive_errors = Number(telemetry.consecutive_errors || 0) + 1;
                telemetry.last_error_user_turns = userTurns;
                telemetry.pending_started_ms = null;
                telemetry.generation_seen = false;
              } else if (telemetry.pending_started_ms !== null && assistantAdvanced) {
                const elapsedMs = Math.max(0, Math.floor(nowMs - Number(telemetry.pending_started_ms || nowMs)));
                const streamedComplete = Boolean(telemetry.generation_seen) && !responseInProgress;
                const fastComplete = !responseInProgress && elapsedMs >= 2000;
                if (streamedComplete || fastComplete) {
                  telemetry.last_response_latency_ms = elapsedMs;
                  telemetry.pending_started_ms = null;
                  telemetry.generation_seen = false;
                  telemetry.consecutive_errors = 0;
                }
              }
              telemetry.last_user_turns = userTurns;
              telemetry.last_assistant_turns = assistantTurns;
              telemetry.url = location.href;
            }
            const currentLatencyMs = telemetry.pending_started_ms === null
              ? 0
              : Math.max(0, Math.floor(nowMs - Number(telemetry.pending_started_ms || nowMs)));
            const responseIdleMs = telemetry.pending_started_ms === null
              ? 0
              : Math.max(0, Math.floor(nowMs - Number(telemetry.last_progress_ms || telemetry.pending_started_ms || nowMs)));
            return {
              user_turns: userTurns,
              assistant_turns: assistantTurns,
              response_in_progress: responseInProgress,
              response_pending: telemetry.pending_started_ms !== null,
              response_idle_ms: responseIdleMs,
              tool_activity_count: toolIcons.length,
              current_response_latency_ms: currentLatencyMs,
              last_response_latency_ms: Math.max(0, Number(telemetry.last_response_latency_ms || 0)),
              consecutive_errors: Math.max(0, Number(telemetry.consecutive_errors || 0)),
            };
          };
          if (!window[observerKey]) {
            const observer = new MutationObserver(() => {
              try { sampleTelemetry(); } catch (_error) { }
            });
            observer.observe(document.documentElement, {subtree: true, childList: true, characterData: true});
            window[observerKey] = observer;
          }
          const telemetry = sampleTelemetry();
          return JSON.stringify({
            ready: Boolean(composer) && authenticatedHint && pageSettled,
            composer_ready: Boolean(composer),
            authenticated_hint: authenticatedHint,
            login_controls: loginControls.length,
            title: document.title || '',
            url: location.href,
            user_turns: telemetry.user_turns,
            assistant_turns: telemetry.assistant_turns,
            response_in_progress: telemetry.response_in_progress,
            response_pending: telemetry.response_pending,
            response_idle_ms: telemetry.response_idle_ms,
            tool_activity_count: telemetry.tool_activity_count,
            current_response_latency_ms: telemetry.current_response_latency_ms,
            last_response_latency_ms: telemetry.last_response_latency_ms,
            consecutive_errors: telemetry.consecutive_errors,
            telemetry_observer_active: Boolean(window[observerKey]),
            page_age_minutes: Math.max(0, Math.floor(performance.now() / 60000)),
            interaction_required: !authenticatedHint || !composer || /ci siamo quasi/i.test(document.title || ''),
            composer_kind: composer ? (composer.id || composer.tagName || '').toLowerCase() : null,
            composer_chars: composer ? String(composer.value || composer.innerText || composer.textContent || '').length : 0,
            document_ready_state: document.readyState,
            page_age_ms: pageAgeMs,
            page_settled: pageSettled,
            temporary_access_limited: temporaryAccessLimited,
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
        auth_result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {
                "expression": (
                    "fetch(\"/api/auth/session\",{credentials:\"same-origin\"})"
                    ".then(r=>r.ok?r.json():null)"
                    ".then(s=>JSON.stringify({authenticated:Boolean(s&&s.user)}))"
                    ".catch(()=>JSON.stringify({authenticated:false}))"
                ),
                "returnByValue": True,
                "awaitPromise": True,
            },
        )
        auth_raw = (auth_result.get("result") or {}).get("value")
        try:
            auth_state = json.loads(auth_raw) if isinstance(auth_raw, str) else {}
        except json.JSONDecodeError:
            auth_state = {}
        authenticated = bool(auth_state.get("authenticated")) if isinstance(auth_state, dict) else False
        state["authenticated"] = authenticated
        access_limited = bool(state.get("temporary_access_limited"))
        state["ready"] = bool(state.get("composer_ready")) and authenticated and bool(state.get("page_settled")) and not access_limited
        state["interaction_required"] = (
            access_limited
            or not authenticated
            or not bool(state.get("composer_ready"))
            or "ci siamo quasi" in str(state.get("title") or "").lower()
        )
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
        focus_expression = r"""(() => {
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled;
          };
          const selectors = ['#prompt-textarea', 'textarea', '[contenteditable="true"]'];
          let el = null;
          for (const selector of selectors) {
            el = Array.from(document.querySelectorAll(selector)).find(node => visible(node) && node.id !== 'bottazzi-human-composer') || null;
            if (el) break;
          }
          if (!el) return JSON.stringify({ok:false, reason:'composer_not_found'});
          el.focus();
          if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) {
            el.select();
          } else {
            const selection = window.getSelection();
            const range = document.createRange();
            range.selectNodeContents(el);
            selection.removeAllRanges();
            selection.addRange(range);
          }
          return JSON.stringify({ok:true});
        })()"""
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": focus_expression, "returnByValue": True},
        )
        raw = (result.get("result") or {}).get("value")
        try:
            focused = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("prompt_focus_invalid") from exc
        if not isinstance(focused, dict) or not focused.get("ok"):
            reason = focused.get("reason", "unknown") if isinstance(focused, dict) else "unknown"
            raise CdpError(f"prompt_focus_failed:{reason}")
        self._page_call(target.websocket_url, "Input.insertText", {"text": prompt})
        submit_confirmed = False
        submit_method: str | None = None
        if submit:
            click_expression = r"""(() => {
              const visible = (el) => {
                if (!el || el.disabled) return false;
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
              };
              const selectors = [
                'button[data-testid="send-button"]',
                'button[aria-label*="Send"]',
                'button[aria-label*="send"]',
                'button[aria-label*="Invia"]',
                'button[aria-label*="invia"]',
              ];
              let button = null;
              for (const selector of selectors) {
                button = Array.from(document.querySelectorAll(selector)).find(visible) || null;
                if (button) break;
              }
              if (!button) return JSON.stringify({clicked:false});
              button.click();
              return JSON.stringify({clicked:true});
            })()"""
            click_deadline = time.monotonic() + 3.0
            while time.monotonic() < click_deadline and submit_method is None:
                click_result = self._page_call(
                    target.websocket_url,
                    "Runtime.evaluate",
                    {"expression": click_expression, "returnByValue": True},
                )
                click_raw = (click_result.get("result") or {}).get("value")
                try:
                    click_state = json.loads(click_raw) if isinstance(click_raw, str) else {}
                except json.JSONDecodeError:
                    click_state = {}
                if isinstance(click_state, dict) and click_state.get("clicked"):
                    submit_method = "button_js"
                    break
                time.sleep(0.25)
            if submit_method is None:
                common = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
                self._page_call(target.websocket_url, "Input.dispatchKeyEvent", {"type": "keyDown", **common})
                self._page_call(target.websocket_url, "Input.dispatchKeyEvent", {"type": "keyUp", **common})
                submit_method = "enter"
            confirm_deadline = time.monotonic() + 30.0
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
                diagnostic_expression = r"""(() => {
                  const composer = document.querySelector('#prompt-textarea') || document.querySelector('textarea') || document.querySelector('[contenteditable="true"]');
                  const send = document.querySelector('button[data-testid="send-button"]') || document.querySelector('button[aria-label*="Send"]') || document.querySelector('button[aria-label*="send"]') || document.querySelector('button[aria-label*="Invia"]') || document.querySelector('button[aria-label*="invia"]');
                  const text = composer ? (composer.value || composer.innerText || composer.textContent || '') : '';
                  return JSON.stringify({
                    composer_chars: text.length,
                    send_found: Boolean(send),
                    send_disabled: send ? Boolean(send.disabled) : null,
                    url: location.href,
                    user_turns: document.querySelectorAll('[data-message-author-role="user"]').length,
                  });
                })()"""
                diagnostic_result = self._page_call(
                    target.websocket_url,
                    "Runtime.evaluate",
                    {"expression": diagnostic_expression, "returnByValue": True},
                )
                diagnostic_raw = (diagnostic_result.get("result") or {}).get("value")
                try:
                    diagnostic = json.loads(diagnostic_raw) if isinstance(diagnostic_raw, str) else {}
                except json.JSONDecodeError:
                    diagnostic = {}
                raise CdpError(f"submit_not_confirmed:{submit_method}:{json.dumps(diagnostic, sort_keys=True)}")
        return {"target_id": target.target_id, "injected": True, "submitted": submit_confirmed, "submit_confirmed": submit_confirmed, "submit_method": submit_method}

    def handoff_to_new_chat(
        self,
        prompt: str,
        *,
        source_target_id: str | None = None,
        submit: bool = True,
        close_source: bool = True,
        target_created_hook: Callable[[str], None] | None = None,
    ) -> dict[str, Any]:
        previous = [target for target in self.targets() if target.target_type == "page" and target.is_chatgpt]
        if source_target_id is None:
            if len(previous) != 1:
                raise CdpError("handoff_source_ambiguous")
            source_target_id = previous[0].target_id
        elif not any(target.target_id == source_target_id for target in previous):
            raise CdpError("handoff_source_not_found")
        target_id = self.create_target("about:blank")
        try:
            if target_created_hook is not None:
                target_created_hook(target_id)
            target = self._wait_target(target_id)
            if not target.websocket_url:
                raise CdpError("new_target_missing_websocket")
            self._page_call(target.websocket_url, "Network.enable")
            self._page_call(target.websocket_url, "Network.clearBrowserCache")
            self._page_call(target.websocket_url, "Page.enable")
            self._page_call(target.websocket_url, "Page.navigate", {"url": CHATGPT_ORIGIN})
            injected = self.inject_prompt(prompt, target_id=target_id, submit=submit)
        except Exception:
            try:
                self.close_target(target_id)
            except CdpError:
                pass
            raise
        closed: list[str] = []
        if close_source and source_target_id != target_id:
            self.close_target(source_target_id)
            closed.append(source_target_id)
        return {
            **injected,
            "new_target_id": target_id,
            "closed_target_ids": closed,
            "server_chat_deleted": False,
            "cache_cleared": True,
        }

    def conversation_urls(
        self,
        target_id: str,
        *,
        reload: bool = False,
        wait_timeout_s: float = 20.0,
    ) -> list[str]:
        target = self._wait_target(target_id)
        if not target.websocket_url:
            raise CdpError("conversation_scan_target_invalid")
        deadline = time.monotonic() + wait_timeout_s
        while time.monotonic() < deadline and not target.is_chatgpt:
            time.sleep(0.25)
            target = self._wait_target(target_id)
        if not target.is_chatgpt or not target.websocket_url:
            raise CdpError("conversation_scan_target_invalid")
        if reload:
            self._page_call(target.websocket_url, "Page.enable")
            self._page_call(target.websocket_url, "Page.reload", {"ignoreCache": True})
        expression = r"""(() => {
          const urls = [];
          const seen = new Set();
          for (const anchor of document.querySelectorAll('a[href]')) {
            const href = String(anchor.href || '');
            if (!/^https:\/\/chatgpt\.com\/(?:g\/[^/]+\/)?c\/[A-Za-z0-9-]+(?:[/?#].*)?$/.test(href)) continue;
            const normalized = href.replace(/[?#].*$/, '').replace(/\/$/, '');
            if (seen.has(normalized)) continue;
            seen.add(normalized);
            urls.push(normalized);
          }
          return JSON.stringify({ready: document.readyState === 'complete', urls});
        })()"""
        last_urls: list[str] = []
        authenticated_seen = False
        while time.monotonic() < deadline:
            try:
                state = self.chatgpt_ui_state(target_id)
            except CdpError:
                time.sleep(0.25)
                continue
            if state.get("temporary_access_limited"):
                raise CdpError("temporary_access_limited")
            if not state.get("authenticated"):
                time.sleep(0.25)
                continue
            authenticated_seen = True
            if not state.get("page_settled"):
                time.sleep(0.25)
                continue
            target = self._wait_target(target_id)
            if not target.websocket_url:
                raise CdpError("conversation_scan_target_missing_websocket")
            result = self._page_call(
                target.websocket_url,
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
            )
            raw = (result.get("result") or {}).get("value")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                payload = {}
            values = payload.get("urls") if isinstance(payload, dict) else []
            if isinstance(values, list):
                last_urls = []
                for value in values:
                    normalized = _canonical_chatgpt_conversation_url(str(value))
                    if normalized and normalized not in last_urls:
                        last_urls.append(normalized)
            if payload.get("ready") and last_urls:
                return last_urls
            time.sleep(0.25)
        if last_urls:
            return last_urls
        if not authenticated_seen:
            raise CdpError("conversation_scan_unauthenticated")
        raise CdpError("conversation_scan_timeout")

    def create_chatgpt_target(self, *, clear_cache: bool = False, background: bool = False) -> str:
        target_id = self.create_target("about:blank", background=background)
        target = self._wait_target(target_id)
        if not target.websocket_url:
            raise CdpError("new_target_missing_websocket")
        self._page_call(target.websocket_url, "Network.enable")
        if clear_cache:
            self._page_call(target.websocket_url, "Network.clearBrowserCache")
        self._page_call(target.websocket_url, "Page.enable")
        self._page_call(target.websocket_url, "Page.navigate", {"url": CHATGPT_ORIGIN})
        return target_id

    def navigate_chatgpt_conversation(
        self,
        target_id: str,
        url: str,
        *,
        wait_timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        normalized = _canonical_chatgpt_conversation_url(url)
        if not normalized:
            raise CdpError("conversation_url_invalid")
        target = self._wait_target(target_id)
        navigation_deadline = time.monotonic() + wait_timeout_s
        while time.monotonic() < navigation_deadline and not target.is_chatgpt:
            time.sleep(0.25)
            target = self._wait_target(target_id)
        if not target.is_chatgpt or not target.websocket_url:
            raise CdpError("conversation_navigation_target_invalid")
        self._page_call(target.websocket_url, "Page.enable")
        self._page_call(target.websocket_url, "Page.navigate", {"url": normalized})
        deadline = time.monotonic() + wait_timeout_s
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            try:
                current = self._wait_target(target_id)
                if _canonical_chatgpt_conversation_url(current.url) != normalized:
                    time.sleep(0.25)
                    continue
                last_state = self.chatgpt_ui_state(target_id)
            except CdpError:
                time.sleep(0.25)
                continue
            if last_state.get("ready"):
                return last_state
            time.sleep(0.25)
        raise CdpError(f"conversation_navigation_timeout:{normalized}")

    def archive_chatgpt_conversation(
        self,
        target_id: str,
        url: str,
        *,
        wait_timeout_s: float = 10.0,
        allow_absent: bool = False,
        archive_started_hook: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        normalized = _canonical_chatgpt_conversation_url(url)
        if not normalized:
            raise CdpError("conversation_url_invalid")
        target = self._wait_target(target_id)
        if _canonical_chatgpt_conversation_url(target.url) != normalized or not target.websocket_url:
            raise CdpError("conversation_archive_target_mismatch")
        ui = self.chatgpt_ui_state(target_id)
        if not ui.get("authenticated") or not ui.get("ready"):
            raise CdpError("conversation_archive_target_not_ready")

        expression = r'''(() => {
          const wanted = %s;
          const normalize = value => {
            try {
              const u = new URL(String(value || ''), location.origin);
              const m = u.pathname.match(/^\/(?:g\/[^/]+\/)?c\/([A-Za-z0-9-]+)/);
              return m ? `${u.origin}/c/${m[1]}` : '';
            } catch (_) { return ''; }
          };
          const links = [...document.querySelectorAll('#history a[href], nav a[href]')]
            .filter(a => normalize(a.href));
          const exact = links.find(a => normalize(a.href) === wanted);
          if (!exact) {
            if (links.length) return JSON.stringify({state: 'absent'});
            const openSidebar = document.querySelector('button[aria-label="Open sidebar"], button[aria-label="Apri barra laterale"]');
            if (openSidebar) {
              openSidebar.click();
              return JSON.stringify({state: 'sidebar_opening'});
            }
            return JSON.stringify({state: 'history_unavailable'});
          }
          let row = exact.parentElement;
          while (row && row !== document.body) {
            const options = row.querySelector('button[data-testid^="history-item-"][data-testid$="-options"]');
            if (options) {
              options.click();
              return JSON.stringify({state: 'menu_opened'});
            }
            row = row.parentElement;
          }
          return JSON.stringify({state: 'options_missing'});
        })()''' % json.dumps(normalized)
        deadline = time.monotonic() + wait_timeout_s
        state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
            raw = (result.get("result") or {}).get("value")
            try:
                state = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                state = {}
            if state.get("state") == "absent":
                if allow_absent:
                    return {"archived": True, "already_archived": True, "conversation_url": normalized}
                raise CdpError("conversation_archive_source_not_in_history")
            if state.get("state") == "menu_opened":
                break
            if state.get("state") == "options_missing":
                raise CdpError("conversation_archive_menu_failed:options_missing")
            time.sleep(0.1)
        else:
            raise CdpError(f"conversation_archive_menu_failed:{state.get('state') or 'invalid'}")

        if archive_started_hook is not None:
            archive_started_hook()

        deadline = time.monotonic() + wait_timeout_s
        click_expression = r'''(() => {
          const labels = new Set(['archive', 'archive chat', 'archivia', 'archivia chat']);
          const items = [...document.querySelectorAll('[role="menuitem"], [role="menuitemradio"], button')];
          const archive = items.find(el => labels.has((el.innerText || el.textContent || '').trim().toLowerCase()));
          if (!archive) return JSON.stringify({clicked: false});
          archive.click();
          return JSON.stringify({clicked: true});
        })()'''
        clicked = False
        while time.monotonic() < deadline:
            click_result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": click_expression, "returnByValue": True})
            click_raw = (click_result.get("result") or {}).get("value")
            try:
                payload = json.loads(click_raw) if isinstance(click_raw, str) else {}
            except json.JSONDecodeError:
                payload = {}
            if payload.get("clicked"):
                clicked = True
                break
            time.sleep(0.1)
        if not clicked:
            raise CdpError("conversation_archive_action_missing")

        verify_expression = r'''(() => {
          const wanted = %s;
          const normalize = value => {
            try {
              const u = new URL(String(value || ''), location.origin);
              const m = u.pathname.match(/^\/(?:g\/[^/]+\/)?c\/([A-Za-z0-9-]+)/);
              return m ? `${u.origin}/c/${m[1]}` : '';
            } catch (_) { return ''; }
          };
          return ![...document.querySelectorAll('#history a[href], nav a[href]')].some(a => normalize(a.href) === wanted);
        })()''' % json.dumps(normalized)
        while time.monotonic() < deadline:
            verify = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": verify_expression, "returnByValue": True})
            if (verify.get("result") or {}).get("value") is True:
                return {"archived": True, "already_archived": False, "conversation_url": normalized}
            time.sleep(0.1)
        raise CdpError("conversation_archive_not_confirmed")

    def chatgpt_focus_state(self, target_id: str) -> dict[str, Any]:
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("focus_target_invalid")
        expression = r'''(() => JSON.stringify({
          focused: document.hasFocus() && document.visibilityState === 'visible',
          visible: document.visibilityState === 'visible',
          ghost: Boolean(window.__bottazziGhostTabV1),
          ghost_close_at: Number((window.__bottazziGhostTabV1 || {}).close_at || 0),
          handoff_locked: Boolean(window.__bottazziHandoffLockV1),
        }))()'''
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("focus_state_invalid") from exc
        if not isinstance(state, dict):
            raise CdpError("focus_state_invalid")
        return state

    def lock_human_input_during_handoff(self, target_id: str) -> dict[str, Any]:
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("handoff_lock_target_invalid")
        expression = r'''(() => {
          const stateKey = '__bottazziHandoffLockV1';
          const overlayId = 'bottazzi-handoff-lock';
          if (window[stateKey]) return JSON.stringify({ok:true, locked:true, already:true});
          const native = document.querySelector('#prompt-textarea') || [...document.querySelectorAll('textarea,[contenteditable="true"]')].find(el => el.id !== 'bottazzi-human-composer') || null;
          const state = {native, pointerEvents:null, tabIndex:null, opacity:null, handlers:[]};
          if (native) {
            state.pointerEvents = native.style.pointerEvents;
            state.tabIndex = native.getAttribute('tabindex');
            state.opacity = native.style.opacity;
            native.blur();
            native.style.pointerEvents = 'none';
            native.style.opacity = '0.35';
            native.tabIndex = -1;
          }
          const block = event => {
            const overlay = document.getElementById(overlayId);
            if (overlay && overlay.contains(event.target)) return;
            event.preventDefault();
            event.stopImmediatePropagation();
          };
          for (const type of ['keydown','beforeinput','paste','drop']) {
            document.addEventListener(type, block, true);
            state.handlers.push([type, block]);
          }
          let overlay = document.getElementById(overlayId);
          if (!overlay && document.body) {
            overlay = document.createElement('div');
            overlay.id = overlayId;
            overlay.textContent = 'Passaggio a una nuova chat in corso. La tastiera qui è temporaneamente bloccata per non perdere quello che scrivi.';
            overlay.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:2147483647;width:min(760px,calc(100vw - 32px));padding:12px 14px;border-radius:12px;background:#5b1a1a;color:#fff;font:700 14px/1.35 system-ui,sans-serif;box-shadow:0 4px 18px rgba(0,0,0,.28)';
            document.body.appendChild(overlay);
          }
          window[stateKey] = state;
          return JSON.stringify({ok:true, locked:true, native_found:Boolean(native), overlay:Boolean(overlay)});
        })()'''
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("handoff_lock_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError("handoff_lock_failed")
        return state

    def unlock_human_input_after_failed_handoff(self, target_id: str) -> dict[str, Any]:
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("handoff_unlock_target_invalid")
        expression = r'''(() => {
          const stateKey = '__bottazziHandoffLockV1';
          const overlayId = 'bottazzi-handoff-lock';
          const state = window[stateKey];
          if (state && Array.isArray(state.handlers)) {
            for (const item of state.handlers) {
              if (Array.isArray(item) && item.length === 2) document.removeEventListener(item[0], item[1], true);
            }
          }
          if (state && state.native) {
            state.native.style.pointerEvents = state.pointerEvents || '';
            state.native.style.opacity = state.opacity || '';
            if (state.tabIndex === null) state.native.removeAttribute('tabindex');
            else state.native.setAttribute('tabindex', state.tabIndex);
          }
          const overlay = document.getElementById(overlayId);
          if (overlay) overlay.remove();
          try { delete window[stateKey]; } catch (_) { window[stateKey] = null; }
          return JSON.stringify({ok:true, locked:false});
        })()'''
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("handoff_unlock_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError("handoff_unlock_failed")
        return state

    def install_human_input_target(self, target_id: str, conversation_url: str) -> dict[str, Any]:
        normalized = _canonical_chatgpt_conversation_url(conversation_url)
        if not normalized:
            raise CdpError("human_input_target_url_invalid")
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("human_input_target_invalid")
        config = json.dumps({"conversation_url": normalized}, ensure_ascii=False)
        expression = r'''(() => {
          const config = __CONFIG__;
          const stateKey = '__bottazziHumanInputTargetV2';
          const draftKey = '__bottazziHumanDraftV2';
          const activeKey = '__bottazziActiveConversationV2';
          const panelId = 'bottazzi-human-panel';
          const humanBoxId = 'bottazzi-human-composer';
          const sendId = 'bottazzi-human-send';
          const statusId = 'bottazzi-human-status';
          const canonical = value => {
            try {
              const u = new URL(String(value || ''), location.origin);
              const m = u.pathname.match(/^\/(?:g\/[^/]+\/)?c\/([A-Za-z0-9-]+)/);
              return m ? `${u.origin}/c/${m[1]}` : '';
            } catch (_) { return ''; }
          };
          const visible = el => {
            if (!el || el.disabled) return false;
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          };
          const findComposer = () => {
            for (const selector of ['#prompt-textarea', 'textarea', '[contenteditable="true"]']) {
              const el = [...document.querySelectorAll(selector)].find(node => node.id !== humanBoxId && visible(node));
              if (el) return el;
            }
            return null;
          };
          const findSend = () => {
            for (const selector of ['button[data-testid="send-button"]','button[aria-label*="Send"]','button[aria-label*="send"]','button[aria-label*="Invia"]','button[aria-label*="invia"]']) {
              const el = [...document.querySelectorAll(selector)].find(visible);
              if (el) return el;
            }
            return null;
          };
          const setNativeText = (composer, text) => {
            composer.focus();
            if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
              const proto = composer instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
              if (!descriptor || !descriptor.set) return false;
              descriptor.set.call(composer, text);
              composer.dispatchEvent(new Event('input', {bubbles:true}));
            } else {
              const sel = window.getSelection();
              const range = document.createRange();
              range.selectNodeContents(composer);
              sel.removeAllRanges();
              sel.addRange(range);
              document.execCommand('insertText', false, text);
              composer.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
            }
            return true;
          };
          const hasPendingDraft = () => {
            try { return Boolean(localStorage.getItem(draftKey)); } catch (_) { return false; }
          };
          const updateHumanUi = () => {
            const box = document.getElementById(humanBoxId);
            const send = document.getElementById(sendId);
            const status = document.getElementById(statusId);
            const pending = hasPendingDraft();
            if (box && box.disabled !== pending) box.disabled = pending;
            if (send && send.disabled !== pending) send.disabled = pending;
            const label = pending ? 'Messaggio in attesa di invio…' : 'Invio umano → chat attiva';
            if (status && status.textContent !== label) status.textContent = label;
          };
          const importDraft = () => {
            let raw = null;
            try { raw = localStorage.getItem(draftKey); } catch (_) { return false; }
            if (!raw) { updateHumanUi(); return false; }
            let payload = null;
            try { payload = JSON.parse(raw); } catch (_) { return false; }
            if (!payload || canonical(payload.successor_url) !== config.conversation_url || canonical(location.href) !== config.conversation_url) return false;
            const text = String(payload.text || '');
            if (!text) return false;
            const composer = findComposer();
            if (!composer || !setNativeText(composer, text)) return false;
            if (payload.submit !== false) {
              const send = findSend();
              if (!send) return false;
              send.click();
            }
            try { localStorage.removeItem(draftKey); } catch (_) {}
            updateHumanUi();
            return true;
          };
          const ensureHumanUi = () => {
            if (!document.body || document.getElementById(panelId)) return;
            const panel = document.createElement('div');
            panel.id = panelId;
            panel.style.cssText = 'position:fixed;left:50%;bottom:12px;transform:translateX(-50%);z-index:2147483646;width:min(760px,calc(100vw - 32px));padding:8px 10px;border-radius:14px;background:rgba(30,30,30,.96);color:#fff;font:600 13px/1.3 system-ui,sans-serif;box-shadow:0 5px 22px rgba(0,0,0,.32)';
            const status = document.createElement('div');
            status.id = statusId;
            status.style.cssText = 'margin:0 2px 6px;opacity:.8;font-size:12px';
            const row = document.createElement('div');
            row.style.cssText = 'display:flex;gap:8px;align-items:flex-end';
            const input = document.createElement('textarea');
            input.id = humanBoxId;
            input.rows = 2;
            input.placeholder = 'Scrivi a Bot-tazzi…';
            input.style.cssText = 'flex:1;resize:vertical;max-height:180px;padding:9px 10px;border-radius:10px;border:1px solid rgba(255,255,255,.25);background:#fff;color:#111;font:14px/1.35 system-ui,sans-serif';
            const send = document.createElement('button');
            send.id = sendId;
            send.type = 'button';
            send.textContent = 'Invia';
            send.style.cssText = 'padding:10px 14px;border-radius:10px;border:0;cursor:pointer;font-weight:800';
            const sendHuman = () => {
              const text = input.value.trim();
              if (!text || hasPendingDraft()) return;
              try {
                localStorage.setItem(draftKey, JSON.stringify({draft_id:`${Date.now()}-${Math.random()}`, successor_url:config.conversation_url, text, submit:true, created_at:Date.now()}));
              } catch (_) { return; }
              input.value = '';
              updateHumanUi();
              setTimeout(importDraft, 0);
            };
            send.addEventListener('click', sendHuman);
            input.addEventListener('keydown', event => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                sendHuman();
              }
            });
            row.appendChild(input);
            row.appendChild(send);
            panel.appendChild(status);
            panel.appendChild(row);
            document.body.appendChild(panel);
            updateHumanUi();
          };
          const lockNative = () => {
            ensureHumanUi();
            const composer = findComposer();
            if (!composer) return;
            composer.setAttribute('data-bottazzi-native-composer', '1');
            composer.style.pointerEvents = 'none';
            composer.style.opacity = '0.22';
            composer.tabIndex = -1;
            const send = findSend();
            if (send) {
              send.style.pointerEvents = 'none';
              send.tabIndex = -1;
            }
          };
          window.name = 'bottazzi-active';
          try { localStorage.setItem(activeKey, config.conversation_url); } catch (_) {}
          let state = window[stateKey];
          if (!state || typeof state !== 'object') state = {version:2};
          if (!state.onStorage) {
            state.onStorage = event => { if (event.key === draftKey) setTimeout(importDraft, 0); };
            window.addEventListener('storage', state.onStorage);
          }
          if (!state.onFocus) {
            state.onFocus = () => setTimeout(importDraft, 0);
            window.addEventListener('focus', state.onFocus);
          }
          if (!state.observer) {
            state.observer = new MutationObserver(() => { lockNative(); importDraft(); });
            state.observer.observe(document.documentElement, {subtree:true, childList:true});
          }
          state.conversation_url = config.conversation_url;
          window[stateKey] = state;
          lockNative();
          importDraft();
          return JSON.stringify({ok:true, human_input_target:true, conversation_url:config.conversation_url, window_name:window.name, human_composer:Boolean(document.getElementById(humanBoxId)), native_locked:Boolean(findComposer())});
        })()'''.replace("__CONFIG__", config)
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("human_input_target_install_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError("human_input_target_install_failed")
        return state

    def mark_chatgpt_ghost_tab(
        self,
        target_id: str,
        *,
        successor_url: str | None = None,
        notice: str = "Questa chat è stata archiviata. Puoi continuare dal campo Bot-tazzi qui sotto; questa scheda si chiuderà automaticamente.",
        close_after_s: int = 30,
    ) -> dict[str, Any]:
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("ghost_target_invalid")
        successor = _canonical_chatgpt_conversation_url(successor_url or "")
        close_after_ms = max(5, int(close_after_s)) * 1000
        config = json.dumps({"notice": notice, "successor_url": successor, "close_after_ms": close_after_ms}, ensure_ascii=False)
        expression = r'''(() => {
          const config = __CONFIG__;
          const handoffStateKey = '__bottazziHandoffLockV1';
          const handoffOverlayId = 'bottazzi-handoff-lock';
          const handoffState = window[handoffStateKey];
          if (handoffState && Array.isArray(handoffState.handlers)) {
            for (const item of handoffState.handlers) {
              if (Array.isArray(item) && item.length === 2) document.removeEventListener(item[0], item[1], true);
            }
          }
          const handoffOverlay = document.getElementById(handoffOverlayId);
          if (handoffOverlay) handoffOverlay.remove();
          try { delete window[handoffStateKey]; } catch (_) { window[handoffStateKey] = null; }
          const activeState = window.__bottazziHumanInputTargetV2;
          if (activeState && activeState.observer) activeState.observer.disconnect();
          if (activeState && activeState.onStorage) window.removeEventListener('storage', activeState.onStorage);
          if (activeState && activeState.onFocus) window.removeEventListener('focus', activeState.onFocus);
          const activePanel = document.getElementById('bottazzi-human-panel');
          if (activePanel) activePanel.remove();
          try { delete window.__bottazziHumanInputTargetV2; } catch (_) { window.__bottazziHumanInputTargetV2 = null; }
          window.name = '';
          const stateKey = '__bottazziGhostTabV1';
          const bannerId = 'bottazzi-ghost-banner';
          const countdownId = 'bottazzi-ghost-countdown';
          const humanBoxId = 'bottazzi-human-composer';
          const draftKey = '__bottazziHumanDraftV2';
          const activeKey = '__bottazziActiveConversationV2';
          const nativeSelectors = ['#prompt-textarea', 'textarea', '[contenteditable="true"]'];
          let state = window[stateKey];
          if (!state || typeof state !== 'object') state = {version:2};
          if (!Number(state.close_at)) state.close_at = Date.now() + Number(config.close_after_ms || 30000);
          state.version = 2;
          state.config = config;
          window[stateKey] = state;
          const visible = el => {
            if (!el) return false;
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
          };
          const findNativeComposer = () => {
            for (const selector of nativeSelectors) {
              const el = [...document.querySelectorAll(selector)].find(visible);
              if (el && el.id !== humanBoxId) return el;
            }
            return null;
          };
          const ensureUi = () => {
            if (!document.body) return;
            let banner = document.getElementById(bannerId);
            if (banner) return;
            banner = document.createElement('div');
            banner.id = bannerId;
            banner.style.cssText = 'position:fixed;left:50%;top:12px;transform:translateX(-50%);z-index:2147483647;width:min(760px,calc(100vw - 32px));padding:10px 12px;border-radius:12px;background:#5b1a1a;color:#fff;font:600 14px/1.35 system-ui,sans-serif;box-shadow:0 4px 18px rgba(0,0,0,.28)';
            const text = document.createElement('div');
            text.textContent = String(config.notice || 'Chat passata a Bot-tazzi.');
            banner.appendChild(text);
            const countdown = document.createElement('div');
            countdown.id = countdownId;
            countdown.style.cssText = 'margin-top:5px;font-weight:800';
            banner.appendChild(countdown);
            const row = document.createElement('div');
            row.style.cssText = 'display:flex;gap:8px;margin-top:8px';
            const input = document.createElement('textarea');
            input.id = humanBoxId;
            input.rows = 2;
            input.placeholder = 'Scrivi qui per continuare nella chat attiva';
            input.style.cssText = 'flex:1;resize:vertical;padding:8px;border-radius:8px;border:1px solid rgba(255,255,255,.35);background:white;color:#111;font:14px system-ui,sans-serif';
            const send = document.createElement('button');
            send.type = 'button';
            send.textContent = 'Continua';
            send.style.cssText = 'padding:8px 12px;border-radius:8px;border:0;cursor:pointer;font-weight:700';
            const forward = () => {
              const textValue = input.value.trim();
              if (!textValue) return;
              let targetUrl = config.successor_url || '';
              try { targetUrl = localStorage.getItem(activeKey) || targetUrl; } catch (_) {}
              if (!targetUrl) return;
              try {
                localStorage.setItem(draftKey, JSON.stringify({draft_id:`${Date.now()}-${Math.random()}`, successor_url:targetUrl, text:textValue, submit:true, created_at:Date.now()}));
              } catch (_) { return; }
              input.value = '';
              window.open(targetUrl, 'bottazzi-active');
            };
            send.addEventListener('click', forward);
            input.addEventListener('keydown', event => {
              if (event.key === 'Enter' && !event.shiftKey) {
                event.preventDefault();
                forward();
              }
            });
            row.appendChild(input);
            row.appendChild(send);
            banner.appendChild(row);
            document.body.appendChild(banner);
          };
          const updateCountdown = () => {
            ensureUi();
            const countdown = document.getElementById(countdownId);
            if (!countdown) return;
            const remaining = Math.max(0, Math.ceil((Number(state.close_at || 0) - Date.now()) / 1000));
            countdown.textContent = remaining > 0 ? `Chiusura automatica tra ${remaining} s` : 'Chiusura automatica…';
          };
          const requestClose = () => {
            state.close_requested = true;
            updateCountdown();
            try { window.close(); } catch (_) {}
          };
          const lockNative = () => {
            ensureUi();
            const composer = findNativeComposer();
            if (composer) {
              composer.setAttribute('data-bottazzi-ghost-native', '1');
              composer.style.pointerEvents = 'none';
              composer.style.opacity = '0.35';
              composer.tabIndex = -1;
              composer.blur();
              const form = composer.closest('form');
              if (form) form.querySelectorAll('button').forEach(button => {
                button.style.pointerEvents = 'none';
                button.tabIndex = -1;
              });
            }
          };
          if (!state.observer) {
            const observer = new MutationObserver(lockNative);
            observer.observe(document.documentElement, {subtree:true, childList:true});
            state.observer = observer;
          }
          if (!state.countdown_timer) state.countdown_timer = setInterval(updateCountdown, 250);
          const closeDelay = Math.max(0, Number(state.close_at || 0) - Date.now());
          if (!state.close_timer) state.close_timer = setTimeout(requestClose, closeDelay);
          lockNative();
          updateCountdown();
          return JSON.stringify({ok:true, ghost:true, successor_url:config.successor_url || null, human_composer:Boolean(document.getElementById(humanBoxId)), close_at:Number(state.close_at || 0), close_after_ms:Number(config.close_after_ms || 0)});
        })()'''.replace("__CONFIG__", config)
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("ghost_install_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError("ghost_install_failed")
        return state

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

    def create_target(self, url: str, *, background: bool = False) -> str:
        params: dict[str, Any] = {"url": url}
        if background:
            params["background"] = True
        response = self._browser_call("Target.createTarget", params)
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
        ws = None
        try:
            ws = websocket.create_connection(
                websocket_url,
                timeout=self.timeout_s,
                suppress_origin=True,
            )
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
        except CdpError:
            raise
        except (websocket.WebSocketException, OSError, TimeoutError) as exc:
            raise CdpError(f"cdp_transport_error:{method}:{type(exc).__name__}") from exc
        finally:
            if ws is not None:
                ws.close()

    def _http_json(self, path: str) -> Any:
        request = urllib.request.Request(self.endpoint + path, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise CdpError(f"cdp_unavailable:{self.endpoint}") from exc
