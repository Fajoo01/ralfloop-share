from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
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


def _safe_chatgpt_shared_url(value: str) -> str | None:
    try:
        parsed = urllib.parse.urlparse(str(value or "").strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
        return None
    match = re.fullmatch(r"/share/([A-Za-z0-9-]+)", parsed.path.rstrip("/"))
    if not match:
        return None
    return f"https://chatgpt.com/share/{match.group(1)}"


def _chatgpt_project_conversation_parts(value: str) -> tuple[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if host != "chatgpt.com" and not host.endswith(".chatgpt.com"):
        return None
    match = re.fullmatch(
        r"/g/(g-p-[A-Za-z0-9]{32})(?:-[^/]+)?/c/([A-Za-z0-9-]+)",
        parsed.path.rstrip("/"),
    )
    if not match:
        return None
    return match.group(1), match.group(2)


def _safe_chatgpt_conversation_context_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError as exc:
        raise CdpError("conversation_url_invalid") from exc
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if host != "chatgpt.com":
        raise CdpError("conversation_url_invalid")
    if not (re.fullmatch(r"/c/[A-Za-z0-9-]+", path) or re.fullmatch(r"/g/[^/]+/c/[A-Za-z0-9-]+", path)):
        raise CdpError("conversation_url_invalid")
    return f"https://chatgpt.com{path}"


def _safe_chatgpt_new_chat_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlparse(str(value or ""))
    except ValueError as exc:
        raise CdpError("new_chat_url_invalid") from exc
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    if host != "chatgpt.com":
        raise CdpError("new_chat_url_invalid")
    if path == "/":
        return CHATGPT_ORIGIN
    if not re.fullmatch(r"/g/[^/]+/project", path):
        raise CdpError("new_chat_url_invalid")
    return f"https://chatgpt.com{path}"


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
          const telemetryKey = '__bottazziGptTelemetryV3';
          const observerKey = '__bottazziGptTelemetryObserverV3';
          const responseErrorRe = /(?:something went wrong|error generating|network error|there was an error|si è verificato un errore|errore (?:di rete|durante|nella|nel)|riprova|try again)/i;
          const temporaryAccessLimitRe = /(?:temporarily limited access to (?:your )?conversations|temporaneamente (?:limitato )?l['’]?accesso alle conversazioni|attendere qualche minuto prima di riprovare|wait a few minutes before trying again)/i;
          const pageText = String(document.body ? document.body.innerText || '' : '');
          const temporaryAccessLimited = temporaryAccessLimitRe.test(pageText);
          const sampleTelemetry = () => {
            const nowMs = performance.now();
            const userLabelRe = /^(?:hai detto|you said|tu hai detto)\s*:?$/i;
            const assistantLabelRe = /^(?:chatgpt ha detto|chatgpt said)\s*:?$/i;
            const roleLabelRe = /^(?:hai detto|you said|tu hai detto|chatgpt ha detto|chatgpt said)\s*:?$/i;
            const labelledTurns = (matcher) => Array.from(document.querySelectorAll('h4.sr-only'))
              .filter((el) => matcher.test(String(el.textContent || '').trim()));
            const labelledAssistantTurns = () => labelledTurns(assistantLabelRe).map((el) => {
              let node = el.parentElement;
              let best = node;
              for (let i = 0; node && i < 8; i++, node = node.parentElement) {
                const roleLabels = Array.from(node.querySelectorAll('h4.sr-only'))
                  .filter((item) => roleLabelRe.test(String(item.textContent || '').trim()));
                if (roleLabels.length > 1) break;
                best = node;
              }
              return best || el.parentElement || el;
            });
            const pairedAssistantTurns = () => {
              const turns = [];
              for (const label of labelledTurns(userLabelRe)) {
                let child = label.parentElement;
                for (let i = 0; child && child.parentElement && i < 8; i++, child = child.parentElement) {
                  const siblings = Array.from(child.parentElement.children || []);
                  const index = siblings.indexOf(child);
                  if (index < 0) continue;
                  const candidate = siblings.slice(index + 1).find((node) => {
                    const hasUserLabel = Array.from(node.querySelectorAll('h4.sr-only'))
                      .some((item) => userLabelRe.test(String(item.textContent || '').trim()));
                    return !hasUserLabel && Boolean(node.querySelector('[class*="MarkdownRoot"], [data-streaming-response-status]'));
                  });
                  if (candidate) {
                    if (!turns.includes(candidate)) turns.push(candidate);
                    break;
                  }
                }
              }
              return turns;
            };
            const userSections = Array.from(document.querySelectorAll('article[data-turn="user"], section[data-turn="user"]'));
            const userRoleNodes = Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
            const userLabelNodes = labelledTurns(userLabelRe);
            const userNodes = userSections.length ? userSections : (userRoleNodes.length ? userRoleNodes : userLabelNodes);
            const userTurns = userNodes.length;
            const assistantSections = Array.from(document.querySelectorAll('article[data-turn="assistant"], section[data-turn="assistant"]'));
            const assistantRoleNodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
            const assistantLabelNodes = labelledAssistantTurns();
            const assistantNodes = assistantSections.length ? assistantSections : (assistantRoleNodes.length ? assistantRoleNodes : (assistantLabelNodes.length ? assistantLabelNodes : pairedAssistantTurns()));
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
            const assistantContent = lastAssistant
              ? (lastAssistant.querySelector('[data-message-author-role="assistant"] .markdown, [data-message-author-role="assistant"]') || lastAssistant.querySelector('.markdown') || lastAssistant)
              : null;
            const lastAssistantError = Boolean(assistantContent && responseErrorRe.test((assistantContent.innerText || assistantContent.textContent || '').trim()));
            const assistantText = assistantContent ? (assistantContent.innerText || assistantContent.textContent || '') : '';
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
            const progressSignature = [userTurns, assistantProgressSignature, toolIcons.length, toolHash >>> 0, responseInProgress ? 1 : 0].join(':');
            let telemetry = window[telemetryKey];
            const resetTelemetry = !telemetry || typeof telemetry !== 'object' || telemetry.version !== 3 || telemetry.url !== location.href || userTurns < Number(telemetry.last_user_turns || 0) || assistantTurns < Number(telemetry.last_assistant_turns || 0);
            if (resetTelemetry) {
              telemetry = {
                version: 3,
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
            const progressIdleMs = Math.max(0, Math.floor(nowMs - Number(telemetry.last_progress_ms || nowMs)));
            return {
              user_turns: userTurns,
              assistant_turns: assistantTurns,
              response_in_progress: responseInProgress,
              response_pending: telemetry.pending_started_ms !== null,
              response_idle_ms: responseIdleMs,
              progress_idle_ms: progressIdleMs,
              progress_signature: progressSignature,
              tool_activity_count: toolIcons.length,
              current_response_latency_ms: currentLatencyMs,
              last_response_latency_ms: Math.max(0, Number(telemetry.last_response_latency_ms || 0)),
              consecutive_errors: Math.max(0, Number(telemetry.consecutive_errors || 0)),
              goal_reached_marker: assistantText.includes('[[BOTTAZZI_GOAL_REACHED]]'),
            };
          };
          const existingObserver = window[observerKey];
          if (existingObserver && typeof existingObserver.disconnect === 'function') {
            try { existingObserver.disconnect(); } catch (_error) { }
          }
          window[observerKey] = null;
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
            progress_idle_ms: telemetry.progress_idle_ms,
            progress_signature: telemetry.progress_signature,
            tool_activity_count: telemetry.tool_activity_count,
            current_response_latency_ms: telemetry.current_response_latency_ms,
            last_response_latency_ms: telemetry.last_response_latency_ms,
            consecutive_errors: telemetry.consecutive_errors,
            goal_reached_marker: Boolean(telemetry.goal_reached_marker),
            telemetry_observer_active: false,
            page_age_minutes: Math.max(0, Math.floor(performance.now() / 60000)),
            interaction_required: !authenticatedHint || !composer || /ci siamo quasi/i.test(document.title || ''),
            composer_kind: composer ? (composer.id || composer.tagName || '').toLowerCase() : null,
            composer_chars: composer ? String(composer.value || composer.innerText || composer.textContent || '').trim().length : 0,
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

    @staticmethod
    def _new_chat_entry_ready_under_history_limit(state: dict[str, Any]) -> bool:
        if not bool(state.get("temporary_access_limited")):
            return False
        if not (
            bool(state.get("composer_ready"))
            and bool(state.get("authenticated"))
            and bool(state.get("page_settled"))
        ):
            return False
        try:
            _safe_chatgpt_new_chat_url(str(state.get("url") or ""))
        except CdpError:
            return False
        return True

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
        new_chat_entry_ready = False
        while time.monotonic() < deadline:
            state = self.chatgpt_ui_state(target_id)
            new_chat_entry_ready = self._new_chat_entry_ready_under_history_limit(state)
            if state.get("ready") or new_chat_entry_ready:
                break
            time.sleep(0.25)
        if not state.get("ready") and not new_chat_entry_ready:
            reason = "interaction_required" if state.get("interaction_required") else "composer_not_ready"
            raise CdpError(f"chatgpt_not_ready:{reason}")

        target = self._wait_target(str(state["target_id"]))
        baseline_user_turns = int(state.get("user_turns") or 0)
        shared_branch_source = _safe_chatgpt_shared_url(target.url) is not None
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
                if shared_branch_source:
                    branched_url = _canonical_chatgpt_conversation_url(current_target.url)
                    if branched_url:
                        return {
                            "target_id": target.target_id,
                            "injected": True,
                            "submitted": False,
                            "submit_confirmed": False,
                            "submit_method": submit_method,
                            "share_branch_created": True,
                            "conversation_url": branched_url,
                            "conversation_context_url": current_target.url,
                        }
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
        new_chat_url: str = CHATGPT_ORIGIN,
        reuse_source_target: bool = False,
    ) -> dict[str, Any]:
        previous = [target for target in self.targets() if target.target_type == "page" and target.is_chatgpt]
        source_target = None
        if source_target_id is None:
            if len(previous) != 1:
                raise CdpError("handoff_source_ambiguous")
            source_target = previous[0]
            source_target_id = source_target.target_id
        else:
            source_target = next((target for target in previous if target.target_id == source_target_id), None)
            if source_target is None:
                raise CdpError("handoff_source_not_found")
        entry_url = _safe_chatgpt_new_chat_url(new_chat_url)
        target_id = source_target_id if reuse_source_target else self.create_target("about:blank")
        source_context_url = _safe_chatgpt_conversation_context_url(source_target.url)
        cache_cleared = False
        try:
            if target_created_hook is not None:
                target_created_hook(target_id)
            target = self._wait_target(target_id)
            if not target.websocket_url:
                raise CdpError("new_target_missing_websocket")
            self._page_call(target.websocket_url, "Network.enable")
            if not reuse_source_target:
                self._page_call(target.websocket_url, "Network.clearBrowserCache")
                cache_cleared = True
            self._page_call(target.websocket_url, "Page.enable")
            self._page_call(target.websocket_url, "Page.navigate", {"url": entry_url})
            injected = self.inject_prompt(prompt, target_id=target_id, submit=submit)
        except Exception:
            if reuse_source_target:
                try:
                    target = self._wait_target(target_id)
                    if target.websocket_url:
                        self._page_call(target.websocket_url, "Page.enable")
                        self._page_call(target.websocket_url, "Page.navigate", {"url": source_context_url})
                except CdpError:
                    pass
            else:
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
            "cache_cleared": cache_cleared,
            "new_chat_entry_url": entry_url,
            "reused_source_target": bool(reuse_source_target),
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

    def conversation_records(
        self,
        target_id: str,
        *,
        reload: bool = False,
        wait_timeout_s: float = 3.0,
    ) -> list[dict[str, str]]:
        target = self._wait_target(target_id)
        if not target.is_chatgpt or not target.websocket_url:
            raise CdpError("conversation_scan_target_invalid")
        call_timeout = max(0.1, min(float(wait_timeout_s), self.timeout_s))
        if reload:
            self._page_call(target.websocket_url, "Page.enable", timeout_s=call_timeout)
            self._page_call(
                target.websocket_url,
                "Page.reload",
                {"ignoreCache": True},
                timeout_s=call_timeout,
            )
        expression = r"""(() => {
          const rows = [];
          const seen = new Set();
          const normalize = value => {
            try {
              const u = new URL(String(value || ''), location.origin);
              const m = u.pathname.match(/^\/(?:g\/[^/]+\/)?c\/([A-Za-z0-9-]+)/);
              return m ? `${u.origin}/c/${m[1]}` : '';
            } catch (_) { return ''; }
          };
          const anchors = document.querySelectorAll('#history a[href], nav a[href]');
          for (const anchor of anchors) {
            const url = normalize(anchor.href);
            if (!url || seen.has(url)) continue;
            const title = String(anchor.innerText || anchor.textContent || anchor.getAttribute('aria-label') || '')
              .replace(/\s+/g, ' ')
              .trim();
            if (!title || /^(?:vai ai contenuti|skip to content)$/i.test(title)) continue;
            seen.add(url);
            rows.push({url, title});
          }
          return JSON.stringify(rows);
        })()"""
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout_s=call_timeout,
        )
        raw = (result.get("result") or {}).get("value")
        try:
            values = json.loads(raw) if isinstance(raw, str) else []
        except json.JSONDecodeError:
            values = []
        records: list[dict[str, str]] = []
        seen: set[str] = set()
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                url = _canonical_chatgpt_conversation_url(str(value.get("url") or ""))
                if not url or url in seen:
                    continue
                seen.add(url)
                records.append({"url": url, "title": str(value.get("title") or "").strip()})
        return records


    def account_catalog(
        self, target_id: str, *, query: str = "", project_id: str = "",
        cursor: str = "", limit: int = 50,
    ) -> dict[str, Any]:
        """Read native ChatGPT query functions; credentials stay in the page.

        The frontend owns authentication, account selection and pagination.
        Only allowlisted catalog metadata is returned over CDP.
        """
        target = self._wait_target(target_id)
        if not target.is_chatgpt or not target.websocket_url:
            raise CdpError("account_catalog_target_invalid")
        if project_id and project_id != "__none__" and not re.fullmatch(r"g-p-[A-Za-z0-9_-]+", project_id):
            raise CdpError("account_catalog_project_invalid")
        if len(cursor) > 8192 or len(query) > 500:
            raise CdpError("account_catalog_input_invalid")
        if cursor:
            try:
                decoded = json.loads(cursor)
            except (TypeError, ValueError) as exc:
                raise CdpError("account_catalog_cursor_invalid") from exc
            if not isinstance(decoded, dict) or set(decoded) != {"page", "scope"} or not isinstance(decoded.get("scope"), str) or not isinstance(decoded["page"], (str, int)):
                raise CdpError("account_catalog_cursor_invalid")
        expression = Path(__file__).with_name("gpt_account_catalog.js").read_text().replace(
            "__CATALOG_INPUT__", json.dumps({"query": query.strip(), "project_id": project_id,
                                           "cursor": cursor, "limit": max(1, min(int(limit), 100))}))
        result = self._page_call(target.websocket_url, "Runtime.evaluate",
                                {"expression": expression, "awaitPromise": True, "returnByValue": True},
                                timeout_s=max(self.timeout_s, 20.0))
        try:
            payload = json.loads((result.get("result") or {}).get("value", ""))
        except (TypeError, ValueError) as exc:
            raise CdpError("account_catalog_response_invalid") from exc
        if not isinstance(payload, dict):
            raise CdpError("account_catalog_response_invalid")
        if payload.get("error"):
            raise CdpError(payload["error"] if payload["error"] in {"account_catalog_source_unavailable", "account_catalog_cursor_scope_mismatch"} else "account_catalog_response_invalid")
        for row in payload.get("chats", []):
            if _canonical_chatgpt_conversation_url(row.get("url", "")) != _canonical_chatgpt_conversation_url(row.get("context_url", "")):
                raise CdpError("account_catalog_context_mismatch")
            _safe_chatgpt_conversation_context_url(row.get("context_url", ""))
        for row in payload.get("projects", []):
            _safe_chatgpt_new_chat_url(row.get("url", ""))
        return payload

    def sidebar_catalog(
        self,
        target_id: str,
        *,
        wait_timeout_s: float = 5.0,
        max_records: int = 240,
    ) -> dict[str, list[dict[str, str]]]:
        """Collect chat history and ChatGPT projects from the sidebar.

        The sidebar scroll position is restored before returning. Callers should
        prefer a background/non-focused ChatGPT target when available.
        """
        target = self._wait_target(target_id)
        if not target.is_chatgpt or not target.websocket_url:
            raise CdpError("sidebar_catalog_target_invalid")
        call_timeout = max(1.0, min(float(wait_timeout_s), max(self.timeout_s, 5.0)))
        limit = max(1, min(int(max_records), 500))
        expression = r"""(async () => {
          const maxRecords = __LIMIT__;
          const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
          const chatInfo = value => {
            try {
              const u = new URL(String(value || ''), location.origin);
              const m = u.pathname.match(/^\/(?:g\/([^/]+)\/)?c\/([A-Za-z0-9-]+)/);
              if (!m) return null;
              return {
                url: `${u.origin}/c/${m[2]}`,
                context_url: m[1] ? `${u.origin}/g/${m[1]}/c/${m[2]}` : `${u.origin}/c/${m[2]}`,
                project_id: m[1] || '',
              };
            } catch (_) { return null; }
          };
          const projectInfo = value => {
            try {
              const u = new URL(String(value || ''), location.origin);
              const m = u.pathname.match(new RegExp('^/g/(g-p-[A-Za-z0-9_-]+)/project/?$'));
              if (!m) return null;
              return {url: `${u.origin}/g/${m[1]}/project`, project_id: m[1]};
            } catch (_) { return null; }
          };
          const navs = [...document.querySelectorAll('nav')];
          const scrollport = navs.find(n => n.scrollHeight > n.clientHeight && [...n.querySelectorAll('a[href]')].some(a => chatInfo(a.href) || projectInfo(a.href)))
            || navs.find(n => [...n.querySelectorAll('a[href]')].some(a => chatInfo(a.href) || projectInfo(a.href)));
          if (!scrollport) return JSON.stringify({chats: [], projects: [], reason: 'sidebar_scrollport_not_found'});
          const originalTop = scrollport.scrollTop;
          const chats = new Map();
          const projects = new Map();
          const projectToggle = [...document.querySelectorAll('button')].find(button => /^(?:progetti|projects)(?:\s|$)/i.test(clean(button.innerText || button.textContent || button.getAttribute('aria-label') || ''))) || null;
          const projectWasCollapsed = Boolean(projectToggle && projectToggle.getAttribute('aria-expanded') === 'false');
          const collect = () => {
            for (const anchor of scrollport.querySelectorAll('a[href]')) {
              const title = clean(anchor.innerText || anchor.textContent || anchor.getAttribute('aria-label') || '');
              const chat = chatInfo(anchor.href);
              if (chat && !chats.has(chat.url)) {
                chats.set(chat.url, {...chat, title: title || 'Chat GPT'});
              }
            }
            for (const anchor of document.querySelectorAll('a[href]')) {
              const project = projectInfo(anchor.href);
              if (!project || projects.has(project.url)) continue;
              const title = clean(anchor.innerText || anchor.textContent || anchor.getAttribute('aria-label') || '');
              projects.set(project.url, {...project, title: title || 'Progetto ChatGPT'});
            }
          };
          try {
            if (projectWasCollapsed) {
              projectToggle.click();
              await new Promise(resolve => setTimeout(resolve, 250));
            }
            collect();
            if (scrollport.scrollHeight > scrollport.clientHeight) {
              scrollport.scrollTop = 0;
              await new Promise(resolve => setTimeout(resolve, 120));
              collect();
              let stable = 0;
              let previousTop = -1;
              let previousHeight = scrollport.scrollHeight;
              for (let i = 0; i < 80 && chats.size < maxRecords; i++) {
                const step = Math.max(220, Math.floor(scrollport.clientHeight * 0.82));
                scrollport.scrollTop = Math.min(scrollport.scrollHeight, scrollport.scrollTop + step);
                await new Promise(resolve => setTimeout(resolve, 250));
                collect();
                const currentHeight = scrollport.scrollHeight;
                const atEnd = scrollport.scrollTop + scrollport.clientHeight >= currentHeight - 4;
                if (atEnd && currentHeight <= previousHeight + 4 && scrollport.scrollTop === previousTop) stable += 1;
                else if (atEnd && currentHeight <= previousHeight + 4) stable += 1;
                else stable = 0;
                previousTop = scrollport.scrollTop;
                previousHeight = currentHeight;
                if (stable >= 6) break;
              }
            }
          } finally {
            scrollport.scrollTop = originalTop;
            if (projectWasCollapsed && projectToggle) projectToggle.click();
          }
          return JSON.stringify({
            chats: [...chats.values()].slice(0, maxRecords),
            projects: [...projects.values()],
            reason: ''
          });
        })()""".replace("__LIMIT__", str(limit))
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout_s=call_timeout,
        )
        raw = (result.get("result") or {}).get("value")
        try:
            payload = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            payload = {}
        chats: list[dict[str, str]] = []
        seen_chats: set[str] = set()
        for value in payload.get("chats", []) if isinstance(payload, dict) else []:
            if not isinstance(value, dict):
                continue
            url = _canonical_chatgpt_conversation_url(str(value.get("url") or ""))
            if not url or url in seen_chats:
                continue
            context = str(value.get("context_url") or url)
            try:
                context = _safe_chatgpt_conversation_context_url(context)
            except CdpError:
                context = url
            seen_chats.add(url)
            chats.append({
                "url": url,
                "context_url": context,
                "title": str(value.get("title") or "").strip(),
                "project_id": str(value.get("project_id") or "").strip(),
            })
        projects: list[dict[str, str]] = []
        seen_projects: set[str] = set()
        for value in payload.get("projects", []) if isinstance(payload, dict) else []:
            if not isinstance(value, dict):
                continue
            try:
                project_url = _safe_chatgpt_new_chat_url(str(value.get("url") or ""))
            except CdpError:
                continue
            if project_url == CHATGPT_ORIGIN or project_url in seen_projects:
                continue
            seen_projects.add(project_url)
            projects.append({
                "url": project_url,
                "title": str(value.get("title") or "").strip() or "Progetto ChatGPT",
                "project_id": str(value.get("project_id") or "").strip(),
            })
        return {"chats": chats, "projects": projects}

    def conversation_records_deep(
        self,
        target_id: str,
        *,
        wait_timeout_s: float = 5.0,
        max_records: int = 240,
    ) -> list[dict[str, str]]:
        return self.sidebar_catalog(
            target_id,
            wait_timeout_s=wait_timeout_s,
            max_records=max_records,
        )["chats"]

    def project_records(
        self,
        target_id: str,
        *,
        wait_timeout_s: float = 12.0,
    ) -> list[dict[str, str]]:
        """Return project names visible in ChatGPT's project directory.

        Project rows are application controls rather than normal anchors, so the
        canonical URL is resolved lazily only when a project is selected.
        """
        target = self._wait_target(target_id)
        if not target.is_chatgpt or not target.websocket_url:
            raise CdpError("project_scan_target_invalid")
        expression = r"""(() => {
          const rows = [];
          const seen = new Set();
          for (const row of document.querySelectorAll('[role="row"][data-page-table-selectable-row]')) {
            const cell = row.querySelector('[role="gridcell"]');
            const titleNode = cell && (cell.querySelector('.text-token-text-primary') || cell);
            const title = String(titleNode ? (titleNode.innerText || titleNode.textContent || '') : '')
              .replace(/\s+/g, ' ').trim();
            if (!title || seen.has(title)) continue;
            seen.add(title);
            rows.push({title});
          }
          return JSON.stringify({ready: document.readyState === 'complete', rows});
        })()"""
        deadline = time.monotonic() + max(1.0, float(wait_timeout_s))
        last_rows: list[dict[str, str]] = []
        while time.monotonic() < deadline:
            target = self._wait_target(target_id)
            if not target.is_chatgpt or not target.websocket_url:
                raise CdpError("project_scan_target_invalid")
            result = self._page_call(
                target.websocket_url,
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
                timeout_s=min(max(1.0, self.timeout_s), 5.0),
            )
            raw = (result.get("result") or {}).get("value")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                payload = {}
            values = payload.get("rows") if isinstance(payload, dict) else []
            rows: list[dict[str, str]] = []
            if isinstance(values, list):
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    title = str(value.get("title") or "").strip()
                    if title and all(row["title"] != title for row in rows):
                        rows.append({"title": title, "url": "", "project_id": ""})
            if rows:
                return rows
            last_rows = rows
            time.sleep(0.25)
        return last_rows

    def project_conversation_records(
        self,
        target_id: str,
        *,
        project_url: str,
        wait_timeout_s: float = 8.0,
    ) -> list[dict[str, str]]:
        """Read conversations from the currently open ChatGPT project page.

        Only project-context conversation links are considered, so generic
        recent-chat sidebar entries cannot be mistaken for project members.
        """
        canonical_project = _safe_chatgpt_new_chat_url(project_url)
        parsed = urllib.parse.urlparse(canonical_project)
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] != "g":
            raise CdpError("project_url_invalid")
        project_id = parts[1]
        expression = r'''(() => {
          const projectId = __PROJECT_ID__;
          const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
          const rows = [];
          const seen = new Set();
          for (const anchor of document.querySelectorAll('a[href]')) {
            let u;
            try { u = new URL(anchor.href, location.origin); } catch (_) { continue; }
            const m = u.pathname.match(new RegExp('^/g/([^/]+)/c/([A-Za-z0-9-]+)'));
            if (!m) continue;
            const projectSegment = m[1];
            if (!(projectSegment === projectId || projectSegment.startsWith(projectId + '-'))) continue;
            const url = `${u.origin}/c/${m[2]}`;
            const contextUrl = `${u.origin}${u.pathname}`;
            if (seen.has(url)) continue;
            const rawTitle = String(anchor.innerText || anchor.textContent || anchor.getAttribute('aria-label') || '');
            const title = clean(rawTitle.split(/\n/)[0]);
            if (!title) continue;
            seen.add(url);
            rows.push({url, context_url: contextUrl, title});
          }
          return JSON.stringify({ready: document.readyState === 'complete', rows});
        })()'''.replace("__PROJECT_ID__", json.dumps(project_id))
        deadline = time.monotonic() + max(1.0, float(wait_timeout_s))
        while time.monotonic() < deadline:
            target = self._wait_target(target_id)
            if not target.is_chatgpt or not target.websocket_url:
                raise CdpError("project_chat_scan_target_invalid")
            result = self._page_call(
                target.websocket_url,
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
                timeout_s=min(max(1.0, self.timeout_s), 5.0),
            )
            raw = (result.get("result") or {}).get("value")
            try:
                payload = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                payload = {}
            values = payload.get("rows") if isinstance(payload, dict) else []
            rows: list[dict[str, str]] = []
            if isinstance(values, list):
                for value in values:
                    if not isinstance(value, dict):
                        continue
                    url = _canonical_chatgpt_conversation_url(str(value.get("url") or ""))
                    title = str(value.get("title") or "").strip()
                    if not url or not title or any(row["url"] == url for row in rows):
                        continue
                    conversation_id = url.rsplit("/", 1)[-1]
                    context_url = str(value.get("context_url") or "").strip()
                    if not _canonical_chatgpt_conversation_url(context_url):
                        context_url = f"{CHATGPT_ORIGIN}/g/{project_id}/c/{conversation_id}"
                    rows.append({
                        "url": url,
                        "context_url": context_url,
                        "title": title,
                        "project_id": project_id,
                    })
            if rows:
                return rows
            time.sleep(0.35)
        return []

    def resolve_project_url(
        self,
        target_id: str,
        project_name: str,
        *,
        wait_timeout_s: float = 12.0,
    ) -> str:
        wanted = str(project_name or "").strip()
        if not wanted:
            raise CdpError("project_name_required")
        deadline = time.monotonic() + max(1.0, float(wait_timeout_s))
        click_expression = r"""(() => {
          const wanted = __WANTED__;
          const rows = [...document.querySelectorAll('[role="row"][data-page-table-selectable-row]')];
          const row = rows.find(row => {
            const cell = row.querySelector('[role="gridcell"]');
            const titleNode = cell && (cell.querySelector('.text-token-text-primary') || cell);
            const title = String(titleNode ? (titleNode.innerText || titleNode.textContent || '') : '')
              .replace(/\s+/g, ' ').trim();
            return title === wanted;
          });
          if (!row) return JSON.stringify({clicked:false, count:rows.length});
          const cell = row.querySelector('[role="gridcell"]') || row;
          cell.click();
          return JSON.stringify({clicked:true});
        })()""".replace("__WANTED__", json.dumps(wanted, ensure_ascii=False))
        clicked = False
        while time.monotonic() < deadline and not clicked:
            target = self._wait_target(target_id)
            if not target.is_chatgpt or not target.websocket_url:
                raise CdpError("project_resolve_target_invalid")
            result = self._page_call(
                target.websocket_url,
                "Runtime.evaluate",
                {"expression": click_expression, "returnByValue": True},
                timeout_s=min(max(1.0, self.timeout_s), 5.0),
            )
            raw = (result.get("result") or {}).get("value")
            try:
                state = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                state = {}
            clicked = bool(isinstance(state, dict) and state.get("clicked"))
            if not clicked:
                time.sleep(0.25)
        if not clicked:
            raise CdpError("project_not_found")
        while time.monotonic() < deadline:
            target = self._wait_target(target_id)
            try:
                url = _safe_chatgpt_new_chat_url(target.url)
            except CdpError:
                time.sleep(0.2)
                continue
            if url != CHATGPT_ORIGIN:
                return url
            time.sleep(0.2)
        raise CdpError("project_navigation_timeout")

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

    def continue_shared_conversation(
        self,
        share_url: str,
        *,
        background: bool = True,
        wait_timeout_s: float = 25.0,
    ) -> dict[str, Any]:
        shared = _safe_chatgpt_shared_url(share_url)
        if not shared:
            raise CdpError("shared_conversation_url_invalid")
        previous_ids = {target.target_id for target in self.targets()}
        target_id = self.create_target(shared, background=background)
        clicked = False
        click_label = ""
        try:
            deadline = time.monotonic() + max(5.0, min(float(wait_timeout_s), 45.0))
            while time.monotonic() < deadline:
                targets = self.targets()
                for current in targets:
                    canonical = _canonical_chatgpt_conversation_url(current.url)
                    if not canonical:
                        continue
                    if current.target_id == target_id or (clicked and current.target_id not in previous_ids):
                        if click_label == "composer_branch" and current.websocket_url:
                            clear_expression = r'''(() => {
                              const el = document.querySelector('#prompt-textarea') || document.querySelector('textarea') || document.querySelector('[contenteditable="true"]');
                              if (!el) return false;
                              if (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) el.value = '';
                              else el.replaceChildren();
                              el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'deleteContentBackward', data:null}));
                              return true;
                            })()'''
                            try:
                                self._page_call(
                                    current.websocket_url,
                                    "Runtime.evaluate",
                                    {"expression": clear_expression, "returnByValue": True},
                                )
                            except CdpError:
                                pass
                        if current.target_id != target_id:
                            try:
                                self.close_target(target_id)
                            except CdpError:
                                pass
                        return {
                            "new_target_id": current.target_id,
                            "conversation_url": canonical,
                            "conversation_context_url": current.url,
                            "source_share_url": shared,
                            "continued_from_share": True,
                            "continue_control": click_label,
                            "server_chat_deleted": False,
                        }
                current = next((item for item in targets if item.target_id == target_id), None)
                if current is None:
                    time.sleep(0.2)
                    continue
                if clicked or not current.websocket_url:
                    time.sleep(0.25)
                    continue
                expression = r'''(() => {
                  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
                  const visible = el => {
                    if (!el || el.disabled) return false;
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);
                    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
                  };
                  const wanted = /^(?:continue(?: this)? conversation|continue in chatgpt|continua(?: questa)? conversazione|continua in chatgpt)$/i;
                  const controls = [...document.querySelectorAll('button,a,[role="button"]')].filter(visible);
                  const control = controls.find(el => wanted.test(clean(el.innerText || el.textContent || el.getAttribute('aria-label'))));
                  if (!control) return JSON.stringify({clicked:false});
                  const label = clean(control.innerText || control.textContent || control.getAttribute('aria-label'));
                  control.click();
                  return JSON.stringify({clicked:true,label});
                })()'''
                result = self._page_call(
                    current.websocket_url,
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    timeout_s=max(1.0, min(self.timeout_s, 5.0)),
                )
                raw = (result.get("result") or {}).get("value")
                try:
                    state = json.loads(raw) if isinstance(raw, str) else {}
                except json.JSONDecodeError:
                    state = {}
                if state.get("clicked"):
                    clicked = True
                    click_label = str(state.get("label") or "")[:120]
                elif not clicked:
                    try:
                        ui = self.chatgpt_ui_state(target_id)
                    except CdpError:
                        ui = {}
                    if bool(ui.get("composer_ready")) and (
                        int(ui.get("user_turns") or 0) + int(ui.get("assistant_turns") or 0) > 0
                    ):
                        branch = self.inject_prompt(
                            "\u2060",
                            target_id=target_id,
                            submit=True,
                            wait_timeout_s=max(2.0, min(8.0, deadline - time.monotonic())),
                        )
                        if branch.get("share_branch_created"):
                            clicked = True
                            click_label = "composer_branch"
                time.sleep(0.3)
            if clicked:
                raise CdpError("shared_conversation_continue_timeout")
            raise CdpError("shared_conversation_continue_control_missing")
        except Exception:
            try:
                self.close_target(target_id)
            except CdpError:
                pass
            raise

    def start_chatgpt_job(
        self,
        prompt: str,
        *,
        new_chat_url: str = CHATGPT_ORIGIN,
        background: bool = True,
        submit: bool = True,
        wait_timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        """Create an independent ChatGPT conversation for a queued job.

        Unlike handoff_to_new_chat this does not require, reuse, archive or
        close an existing worker tab. This makes it suitable for a bounded
        multi-chat spooler where every job owns its own browser target.
        """
        if not prompt.strip():
            raise CdpError("empty_prompt")
        entry_url = _safe_chatgpt_new_chat_url(new_chat_url)
        target_id = self.create_target("about:blank", background=background)
        try:
            target = self._wait_target(target_id)
            if not target.websocket_url:
                raise CdpError("new_target_missing_websocket")
            self._page_call(target.websocket_url, "Network.enable")
            self._page_call(target.websocket_url, "Page.enable")
            self._page_call(target.websocket_url, "Page.navigate", {"url": entry_url})
            injected = self.inject_prompt(
                prompt,
                target_id=target_id,
                submit=submit,
                wait_timeout_s=wait_timeout_s,
            )
            conversation_url = None
            context_url = None
            deadline = time.monotonic() + max(1.0, min(wait_timeout_s, 15.0))
            while time.monotonic() < deadline:
                current = self._wait_target(target_id)
                context_url = current.url
                conversation_url = _canonical_chatgpt_conversation_url(current.url)
                if conversation_url:
                    break
                time.sleep(0.2)
            return {
                **injected,
                "new_target_id": target_id,
                "new_chat_entry_url": entry_url,
                "conversation_url": conversation_url,
                "conversation_context_url": context_url,
                "background": bool(background),
                "server_chat_deleted": False,
            }
        except Exception:
            try:
                self.close_target(target_id)
            except CdpError:
                pass
            raise

    def _navigate_chatgpt_conversation_via_project(
        self,
        target_id: str,
        context_url: str,
        *,
        wait_timeout_s: float = 12.0,
    ) -> dict[str, Any]:
        parts = _chatgpt_project_conversation_parts(context_url)
        normalized = _canonical_chatgpt_conversation_url(context_url)
        if parts is None or not normalized:
            raise CdpError("conversation_project_context_invalid")
        project_id, conversation_id = parts
        project_url = f"https://chatgpt.com/g/{project_id}/project"
        target = self._wait_target(target_id)
        if not target.websocket_url:
            raise CdpError("conversation_navigation_target_invalid")
        self._page_call(target.websocket_url, "Page.enable")
        project_path = f"/g/{project_id}/project"
        initial_deadline = time.monotonic() + min(2.0, max(0.5, float(wait_timeout_s) / 4.0))
        while time.monotonic() < initial_deadline:
            target = self._wait_target(target_id)
            if target.is_chatgpt and urllib.parse.urlparse(target.url).path.rstrip("/") == project_path:
                break
            time.sleep(0.2)
        else:
            target = self._wait_target(target_id)
            if not target.websocket_url:
                raise CdpError("conversation_navigation_target_invalid")
            self._page_call(target.websocket_url, "Page.navigate", {"url": project_url})
        project_deadline = time.monotonic() + max(2.0, min(float(wait_timeout_s), 6.0))
        while time.monotonic() < project_deadline:
            target = self._wait_target(target_id)
            if target.is_chatgpt and target.websocket_url:
                parsed = urllib.parse.urlparse(target.url)
                if parsed.path.rstrip("/") == f"/g/{project_id}/project":
                    break
            time.sleep(0.25)
        else:
            raise CdpError("conversation_project_page_timeout")
        scan_deadline = time.monotonic() + max(2.0, min(float(wait_timeout_s), 10.0))
        row: dict[str, str] | None = None
        while time.monotonic() < scan_deadline and row is None:
            remaining = max(0.5, scan_deadline - time.monotonic())
            rows = self.project_conversation_records(
                target_id,
                project_url=project_url,
                wait_timeout_s=min(1.5, remaining),
            )
            row = next((item for item in rows if item.get("url") == normalized), None)
            if row is None:
                time.sleep(0.35)
        if row is None:
            raise CdpError("conversation_project_entry_missing")
        target = self._wait_target(target_id)
        if not target.websocket_url:
            raise CdpError("conversation_navigation_target_invalid")
        expression = r'''(() => {
          const wanted = __WANTED__;
          const anchor = [...document.querySelectorAll('a[href]')].find((item) => {
            try {
              const u = new URL(item.href, location.origin);
              return u.pathname.includes('/c/' + wanted);
            } catch (_) { return false; }
          });
          if (!anchor) return JSON.stringify({clicked:false});
          anchor.click();
          return JSON.stringify({clicked:true, href:anchor.href});
        })()'''.replace("__WANTED__", json.dumps(conversation_id))
        clicked = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout_s=max(1.0, min(self.timeout_s, 5.0)),
        )
        raw = (clicked.get("result") or {}).get("value")
        try:
            click_state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            click_state = {}
        if not click_state.get("clicked"):
            raise CdpError("conversation_project_click_missing")
        deadline = time.monotonic() + max(2.0, float(wait_timeout_s))
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            current = self._wait_target(target_id)
            if _canonical_chatgpt_conversation_url(current.url) != normalized:
                time.sleep(0.25)
                continue
            last_state = self.chatgpt_ui_state(target_id)
            if last_state.get("temporary_access_limited"):
                raise CdpError("temporary_access_limited")
            if (
                last_state.get("user_turns", 0) > 0
                or last_state.get("assistant_turns", 0) > 0
                or last_state.get("response_in_progress")
                or last_state.get("response_pending")
            ):
                last_state["recovered_via_project"] = True
                last_state["conversation_context_url"] = current.url
                return last_state
            time.sleep(0.25)
        raise CdpError(f"conversation_project_click_timeout:{normalized}")

    def create_project_conversation_target(
        self,
        context_url: str,
        *,
        background: bool = True,
        wait_timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        parts = _chatgpt_project_conversation_parts(context_url)
        if parts is None:
            raise CdpError("conversation_project_context_invalid")
        project_id, _ = parts
        project_url = f"https://chatgpt.com/g/{project_id}/project"
        target_id = self.create_target(project_url, background=background)
        try:
            state = self._navigate_chatgpt_conversation_via_project(
                target_id,
                context_url,
                wait_timeout_s=wait_timeout_s,
            )
            return {"new_target_id": target_id, **state}
        except Exception:
            try:
                self.close_target(target_id)
            except CdpError:
                pass
            raise

    def _navigate_chatgpt_conversation_via_sidebar(
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
        if not target.websocket_url:
            raise CdpError("conversation_navigation_target_invalid")
        self._page_call(target.websocket_url, "Page.enable")
        self._page_call(target.websocket_url, "Page.navigate", {"url": CHATGPT_ORIGIN})
        home_deadline = time.monotonic() + max(2.0, min(float(wait_timeout_s), 8.0))
        while time.monotonic() < home_deadline:
            target = self._wait_target(target_id)
            if target.is_chatgpt and target.websocket_url:
                break
            time.sleep(0.2)
        else:
            raise CdpError("conversation_sidebar_home_timeout")
        expression = r"""(async () => {
          const wanted = __WANTED__;
          const normalize = value => {
            try {
              const u = new URL(String(value || ''), location.origin);
              const m = u.pathname.match(/^\/(?:g\/[^/]+\/)?c\/([A-Za-z0-9-]+)/);
              return m ? `${u.origin}/c/${m[1]}` : '';
            } catch (_) { return ''; }
          };
          const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
          const openSidebar = [...document.querySelectorAll('button')].find(button => {
            const label = clean(button.getAttribute('aria-label') || button.innerText || button.textContent || '');
            return /^(?:(?:open|show) sidebar|(?:apri|mostra|visualizza) barra laterale)$/i.test(label);
          });
          if (openSidebar) {
            openSidebar.click();
            await new Promise(resolve => setTimeout(resolve, 250));
          }
          const navs = [...document.querySelectorAll('nav')];
          const scrollport = navs.find(n => n.scrollHeight > n.clientHeight && [...n.querySelectorAll('a[href]')].some(a => normalize(a.href)))
            || navs.find(n => [...n.querySelectorAll('a[href]')].some(a => normalize(a.href)));
          if (!scrollport) return JSON.stringify({clicked:false, reason:'sidebar_scrollport_not_found'});
          const clickExact = () => {
            const exact = [...scrollport.querySelectorAll('a[href]')].find(a => normalize(a.href) === wanted);
            if (!exact) return null;
            const href = exact.href;
            exact.click();
            return href;
          };
          let href = clickExact();
          if (href) return JSON.stringify({clicked:true, href});
          scrollport.scrollTop = 0;
          await new Promise(resolve => setTimeout(resolve, 120));
          for (let i = 0; i < 100; i++) {
            href = clickExact();
            if (href) return JSON.stringify({clicked:true, href});
            const before = scrollport.scrollTop;
            const step = Math.max(220, Math.floor(scrollport.clientHeight * 0.82));
            scrollport.scrollTop = Math.min(scrollport.scrollHeight, scrollport.scrollTop + step);
            await new Promise(resolve => setTimeout(resolve, 220));
            if (scrollport.scrollTop === before && scrollport.scrollTop + scrollport.clientHeight >= scrollport.scrollHeight - 4) break;
          }
          return JSON.stringify({clicked:false, reason:'conversation_sidebar_entry_missing'});
        })()""".replace("__WANTED__", json.dumps(normalized))
        target = self._wait_target(target_id)
        clicked = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout_s=max(5.0, min(float(wait_timeout_s), 30.0)),
        )
        raw = (clicked.get("result") or {}).get("value")
        try:
            click_state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError:
            click_state = {}
        if not click_state.get("clicked"):
            raise CdpError(str(click_state.get("reason") or "conversation_sidebar_click_missing"))
        deadline = time.monotonic() + max(4.0, float(wait_timeout_s))
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            current = self._wait_target(target_id)
            if _canonical_chatgpt_conversation_url(current.url) != normalized:
                time.sleep(0.25)
                continue
            last_state = self.chatgpt_ui_state(target_id)
            if last_state.get("temporary_access_limited"):
                raise CdpError("temporary_access_limited")
            if (
                last_state.get("user_turns", 0) > 0
                or last_state.get("assistant_turns", 0) > 0
                or last_state.get("response_in_progress")
                or last_state.get("response_pending")
            ):
                last_state["recovered_via_sidebar"] = True
                last_state["conversation_context_url"] = current.url
                return last_state
            time.sleep(0.25)
        raise CdpError(f"conversation_sidebar_click_timeout:{normalized}")

    def create_sidebar_conversation_target(
        self,
        url: str,
        *,
        background: bool = True,
        wait_timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        target_id = self.create_target(CHATGPT_ORIGIN, background=background)
        try:
            state = self._navigate_chatgpt_conversation_via_sidebar(
                target_id,
                url,
                wait_timeout_s=wait_timeout_s,
            )
            return {"new_target_id": target_id, **state}
        except Exception:
            try:
                self.close_target(target_id)
            except CdpError:
                pass
            raise

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
        context_url = _safe_chatgpt_conversation_context_url(url)
        target = self._wait_target(target_id)
        navigation_deadline = time.monotonic() + wait_timeout_s
        while time.monotonic() < navigation_deadline and not target.is_chatgpt:
            time.sleep(0.25)
            target = self._wait_target(target_id)
        if not target.is_chatgpt or not target.websocket_url:
            raise CdpError("conversation_navigation_target_invalid")
        self._page_call(target.websocket_url, "Page.enable")
        self._page_call(target.websocket_url, "Page.navigate", {"url": context_url})
        deadline = time.monotonic() + wait_timeout_s
        last_state: dict[str, Any] = {}
        project_context = _chatgpt_project_conversation_parts(context_url) is not None
        empty_since: float | None = None
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
            if last_state.get("temporary_access_limited"):
                raise CdpError("temporary_access_limited")
            has_content = bool(
                last_state.get("user_turns", 0) > 0
                or last_state.get("assistant_turns", 0) > 0
                or last_state.get("response_in_progress")
                or last_state.get("response_pending")
            )
            if last_state.get("ready") and has_content:
                return last_state
            if (
                not has_content
                and last_state.get("authenticated")
                and last_state.get("page_settled")
            ):
                empty_since = empty_since or time.monotonic()
                grace = min(1.5, max(0.25, float(wait_timeout_s) / 8.0))
                if time.monotonic() - empty_since >= grace:
                    break
            else:
                empty_since = None
            time.sleep(0.25)
        if project_context:
            return self._navigate_chatgpt_conversation_via_project(
                target_id,
                context_url,
                wait_timeout_s=max(8.0, min(float(wait_timeout_s), 30.0)),
            )
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

    def chatgpt_companion_state(self, target_id: str) -> dict[str, Any]:
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("companion_state_target_invalid")
        expression = r'''(() => {
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled;
          };
          const stopSelectors = [
            'button[data-testid="stop-button"]',
            'button[aria-label*="Stop"]',
            'button[aria-label*="stop"]',
            'button[aria-label*="Interrompi"]',
            'button[aria-label*="interrompi"]',
          ];
          const responseInProgress = stopSelectors.some((selector) => Array.from(document.querySelectorAll(selector)).some(visible));
          const telemetry = window.__bottazziGptTelemetryV3 || window.__bottazziGptTelemetryV2;
          const responsePending = Boolean(telemetry && telemetry.pending_started_ms !== null && telemetry.pending_started_ms !== undefined);
          const composer = Array.from(document.querySelectorAll('#prompt-textarea, textarea, [contenteditable="true"]'))
            .find((el) => visible(el) && el.id !== 'bottazzi-human-composer') || null;
          const composerText = composer ? String(composer.value || composer.innerText || composer.textContent || '').trim() : '';
          const humanComposer = document.getElementById('bottazzi-human-composer');
          const humanComposerText = humanComposer ? String(humanComposer.value || humanComposer.innerText || humanComposer.textContent || '').trim() : '';
          const userLabelRe = /^(?:hai detto|you said|tu hai detto)\s*:?$/i;
          const assistantLabelRe = /^(?:chatgpt ha detto|chatgpt said)\s*:?$/i;
          const roleLabelRe = /^(?:hai detto|you said|tu hai detto|chatgpt ha detto|chatgpt said)\s*:?$/i;
          const allRoleLabels = Array.from(document.querySelectorAll('h4.sr-only'));
          const userLabelNodes = allRoleLabels.filter((el) => userLabelRe.test(String(el.textContent || '').trim()));
          const assistantLabelNodes = allRoleLabels
            .filter((el) => assistantLabelRe.test(String(el.textContent || '').trim()))
            .map((el) => {
              const roleLabel = /^(?:hai detto|you said|tu hai detto|chatgpt ha detto|chatgpt said)\s*:?$/i;
              let node = el.parentElement;
              let best = node;
              for (let i = 0; node && i < 8; i++, node = node.parentElement) {
                const roleLabels = Array.from(node.querySelectorAll('h4.sr-only'))
                  .filter((item) => roleLabelRe.test(String(item.textContent || '').trim()));
                if (roleLabels.length > 1) break;
                best = node;
              }
              return best || el.parentElement || el;
            });
          const pairedAssistantTurns = () => {
            const turns = [];
            for (const label of userLabelNodes) {
              let child = label.parentElement;
              for (let i = 0; child && child.parentElement && i < 8; i++, child = child.parentElement) {
                const siblings = Array.from(child.parentElement.children || []);
                const index = siblings.indexOf(child);
                if (index < 0) continue;
                const candidate = siblings.slice(index + 1).find((node) => {
                  const hasUserLabel = Array.from(node.querySelectorAll('h4.sr-only'))
                    .some((item) => userLabelRe.test(String(item.textContent || '').trim()));
                  return !hasUserLabel && Boolean(node.querySelector('[class*="MarkdownRoot"], [data-streaming-response-status]'));
                });
                if (candidate) {
                  if (!turns.includes(candidate)) turns.push(candidate);
                  break;
                }
              }
            }
            return turns;
          };
          const userSections = Array.from(document.querySelectorAll('article[data-turn="user"], section[data-turn="user"]'));
          const userRoleNodes = Array.from(document.querySelectorAll('[data-message-author-role="user"]'));
          const userNodes = userSections.length ? userSections : (userRoleNodes.length ? userRoleNodes : userLabelNodes);
          const assistantSections = Array.from(document.querySelectorAll('article[data-turn="assistant"], section[data-turn="assistant"]'));
          const assistantRoleNodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
          const assistantNodes = assistantSections.length ? assistantSections : (assistantRoleNodes.length ? assistantRoleNodes : (assistantLabelNodes.length ? assistantLabelNodes : pairedAssistantTurns()));
          const lastAssistant = assistantNodes.length ? assistantNodes[assistantNodes.length - 1] : null;
          const streamingNode = lastAssistant ? lastAssistant.querySelector('[data-streaming-response-status]') : null;
          const assistantContent = lastAssistant
            ? (lastAssistant.querySelector('[data-message-author-role="assistant"] .markdown, [data-message-author-role="assistant"]') || lastAssistant.querySelector('.markdown') || lastAssistant)
            : null;
          const toolActivityCount = lastAssistant ? lastAssistant.querySelectorAll('[data-testid="cot-v5-native-tool-icon"]').length : 0;
          const lastAssistantText = assistantContent
            ? String((responseInProgress && streamingNode ? streamingNode.innerText || streamingNode.textContent : assistantContent.innerText || assistantContent.textContent) || '').trim().slice(-24000)
            : '';
          const statusNodes = Array.from(document.querySelectorAll('[role="status"]')).filter(visible);
          const statusText = statusNodes.length
            ? String(statusNodes[statusNodes.length - 1].innerText || statusNodes[statusNodes.length - 1].textContent || '').trim().slice(-180)
            : '';
          const activityLines = lastAssistantText.split(/\n+/).map(line => line.trim()).filter(line => line && !line.includes('[[BOTTAZZI_GOAL_'));
          const activityText = activityLines.length ? activityLines[activityLines.length - 1].slice(-240) : statusText;
          const nowMs = performance.now();
          const progressIdleMs = telemetry && Number(telemetry.last_progress_ms || 0) > 0
            ? Math.max(0, Math.floor(nowMs - Number(telemetry.last_progress_ms)))
            : 0;
          const responseLatencyMs = telemetry && telemetry.pending_started_ms !== null && telemetry.pending_started_ms !== undefined
            ? Math.max(0, Math.floor(nowMs - Number(telemetry.pending_started_ms || nowMs)))
            : Math.max(0, Number((telemetry || {}).last_response_latency_ms || 0));
          return JSON.stringify({
            focused: document.hasFocus() && document.visibilityState === 'visible',
            visible: document.visibilityState === 'visible',
            ghost: Boolean(window.__bottazziGhostTabV1),
            ghost_close_at: Number((window.__bottazziGhostTabV1 || {}).close_at || 0),
            busy: responseInProgress || responsePending,
            streaming_current: Boolean(responseInProgress && streamingNode && lastAssistantText),
            composer_chars: composerText.length,
            human_composer_chars: humanComposerText.length,
            human_composer_active: Boolean(humanComposer && document.activeElement === humanComposer),
            user_turns: userNodes.length,
            assistant_turns: assistantNodes.length,
            tool_activity_count: toolActivityCount,
            status_text: statusText,
            activity_text: activityText,
            progress_idle_ms: progressIdleMs,
            response_latency_ms: responseLatencyMs,
            last_assistant_text: lastAssistantText,
          });
        })()'''
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout_s=max(1.5, min(self.timeout_s, 3.0)),
        )
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("companion_state_invalid") from exc
        if not isinstance(state, dict):
            raise CdpError("companion_state_invalid")
        return state

    def submit_chatgpt_composer(self, target_id: str, *, wait_timeout_s: float = 8.0) -> dict[str, Any]:
        """Submit an already-populated native ChatGPT composer without altering its text."""
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("composer_submit_target_invalid")
        before = self.chatgpt_ui_state(target_id)
        baseline_user_turns = int(before.get("user_turns") or 0)
        expression = r'''(() => {
          const visible = (el) => {
            if (!el || el.disabled) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const composer = Array.from(document.querySelectorAll('#prompt-textarea, textarea, [contenteditable="true"]'))
            .find((el) => visible(el) && el.id !== 'bottazzi-human-composer') || null;
          const text = composer ? String(composer.value || composer.innerText || composer.textContent || '') : '';
          if (!composer || !text.trim()) return JSON.stringify({submitted:false, reason:'composer_empty', composer_chars:text.length});
          const selectors = [
            'button[data-testid="send-button"]',
            'button[aria-label*="Send"]',
            'button[aria-label*="send"]',
            'button[aria-label*="Invia"]',
            'button[aria-label*="invia"]',
          ];
          let send = null;
          for (const selector of selectors) {
            send = Array.from(document.querySelectorAll(selector)).find(visible) || null;
            if (send) break;
          }
          if (!send) return JSON.stringify({submitted:false, reason:'send_missing', composer_chars:text.length});
          send.click();
          return JSON.stringify({submitted:true, composer_chars:text.length});
        })()'''
        try:
            result = self._page_call(
                target.websocket_url,
                "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
                timeout_s=5.0,
            )
        except CdpError as exc:
            if "cdp_timeout:Runtime.evaluate" not in str(exc) and "cdp_transport_error:Runtime.evaluate:WebSocketTimeoutException" not in str(exc):
                raise
            deadline = time.monotonic() + max(1.0, float(wait_timeout_s))
            while time.monotonic() < deadline:
                current = self.chatgpt_ui_state(target_id)
                if int(current.get("user_turns") or 0) > baseline_user_turns:
                    return {"submitted": True, "confirmed": True, "confirm_reason": "user_turn_advanced_after_submit_timeout"}
                if int(current.get("composer_chars") or 0) == 0 and (
                    bool(current.get("response_in_progress")) or bool(current.get("response_pending"))
                ):
                    return {"submitted": True, "confirmed": True, "confirm_reason": "generation_started_after_submit_timeout"}
                time.sleep(0.2)
            raise
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("composer_submit_invalid") from exc
        if not isinstance(state, dict) or not state.get("submitted"):
            return state if isinstance(state, dict) else {"submitted": False, "reason": "invalid"}
        deadline = time.monotonic() + max(1.0, float(wait_timeout_s))
        while time.monotonic() < deadline:
            current = self.chatgpt_ui_state(target_id)
            if int(current.get("user_turns") or 0) > baseline_user_turns:
                return {**state, "confirmed": True, "confirm_reason": "user_turn_advanced"}
            if int(current.get("composer_chars") or 0) == 0 and (
                bool(current.get("response_in_progress")) or bool(current.get("response_pending"))
            ):
                return {**state, "confirmed": True, "confirm_reason": "generation_started"}
            time.sleep(0.2)
        raise CdpError("composer_submit_not_confirmed")

    def wake_stalled_chatgpt(self, target_id: str, *, text: str = "A che punto sei? Hai risolto?", wait_timeout_s: float = 4.0) -> dict[str, Any]:
        """Probe a stale Stop state by typing a short continuation and submit only if Send becomes available."""
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("wake_stalled_target_invalid")
        before = self.chatgpt_ui_state(target_id)
        baseline_user_turns = int(before.get("user_turns") or 0)
        payload = json.dumps(str(text or "prosegui").strip(), ensure_ascii=False)
        expression = r'''(async () => {
          const text = __TEXT__;
          const visible = (el) => {
            if (!el || el.disabled) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const composer = Array.from(document.querySelectorAll('#prompt-textarea, textarea, [contenteditable="true"]'))
            .find((el) => visible(el) && el.id !== 'bottazzi-human-composer') || null;
          if (!composer) return JSON.stringify({submitted:false, reason:'composer_missing'});
          const existing = String(composer.value || composer.innerText || composer.textContent || '');
          if (existing.trim()) return JSON.stringify({submitted:false, reason:'composer_not_empty', composer_chars:existing.length});
          const stopSelectors = [
            'button[data-testid="stop-button"]',
            'button[aria-label*="Stop"]',
            'button[aria-label*="stop"]',
            'button[aria-label*="Interrompi"]',
            'button[aria-label*="interrompi"]',
          ];
          const hadStop = stopSelectors.some((selector) => Array.from(document.querySelectorAll(selector)).some(visible));
          const setText = (value) => {
            composer.focus();
            if (composer instanceof HTMLTextAreaElement || composer instanceof HTMLInputElement) {
              const proto = composer instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const descriptor = Object.getOwnPropertyDescriptor(proto, 'value');
              if (!descriptor || !descriptor.set) return false;
              descriptor.set.call(composer, value);
              composer.dispatchEvent(new Event('input', {bubbles:true}));
            } else {
              composer.innerHTML = '';
              composer.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'deleteContentBackward'}));
              if (value) {
                const sel = window.getSelection();
                const range = document.createRange();
                range.selectNodeContents(composer);
                sel.removeAllRanges();
                sel.addRange(range);
                document.execCommand('insertText', false, value);
                composer.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:value}));
              }
            }
            return true;
          };
          if (!setText(text)) return JSON.stringify({submitted:false, reason:'composer_write_failed', had_stop:hadStop});
          await new Promise((resolve) => setTimeout(resolve, 180));
          const sendSelectors = [
            'button[data-testid="send-button"]',
            'button[aria-label*="Send"]',
            'button[aria-label*="send"]',
            'button[aria-label*="Invia"]',
            'button[aria-label*="invia"]',
          ];
          let send = null;
          for (const selector of sendSelectors) {
            send = Array.from(document.querySelectorAll(selector)).find(visible) || null;
            if (send) break;
          }
          if (!send) {
            setText('');
            return JSON.stringify({submitted:false, reason:'send_missing_after_wake', had_stop:hadStop});
          }
          send.click();
          return JSON.stringify({submitted:true, woke:true, had_stop:hadStop, text});
        })()
'''.replace('__TEXT__', payload)
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout_s=max(2.0, min(self.timeout_s, 6.0)),
        )
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("wake_stalled_invalid") from exc
        if not isinstance(state, dict):
            raise CdpError("wake_stalled_invalid")
        if not state.get("submitted"):
            return state
        deadline = time.monotonic() + max(1.0, float(wait_timeout_s))
        while time.monotonic() < deadline:
            current = self.chatgpt_ui_state(target_id)
            if int(current.get("user_turns") or 0) > baseline_user_turns:
                return {**state, "confirmed": True, "confirm_reason": "user_turn_advanced"}
            if int(current.get("composer_chars") or 0) == 0:
                return {**state, "confirmed": True, "confirm_reason": "composer_cleared_after_submit"}
            time.sleep(0.2)
        return {**state, "confirmed": False, "confirm_reason": "submit_clicked_unconfirmed"}

    def stop_chatgpt_response(self, target_id: str) -> dict[str, Any]:
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("stop_response_target_invalid")
        expression = r'''(() => {
          const visible = (el) => {
            if (!el) return false;
            const rect = el.getBoundingClientRect();
            const style = window.getComputedStyle(el);
            return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && !el.disabled;
          };
          const selectors = [
            'button[data-testid="stop-button"]',
            'button[aria-label*="Stop"]',
            'button[aria-label*="stop"]',
            'button[aria-label*="Interrompi"]',
            'button[aria-label*="interrompi"]',
          ];
          let stop = null;
          for (const selector of selectors) {
            stop = Array.from(document.querySelectorAll(selector)).find(visible) || null;
            if (stop) break;
          }
          const userLabelRe = /^(?:hai detto|you said|tu hai detto)\s*:?$/i;
          const assistantLabelRe = /^(?:chatgpt ha detto|chatgpt said)\s*:?$/i;
          const roleLabelRe = /^(?:hai detto|you said|tu hai detto|chatgpt ha detto|chatgpt said)\s*:?$/i;
          const allRoleLabels = Array.from(document.querySelectorAll('h4.sr-only'));
          const assistantLabelNodes = allRoleLabels
            .filter((el) => assistantLabelRe.test(String(el.textContent || '').trim()))
            .map((el) => {
              const roleLabel = /^(?:hai detto|you said|tu hai detto|chatgpt ha detto|chatgpt said)\s*:?$/i;
              let node = el.parentElement;
              let best = node;
              for (let i = 0; node && i < 8; i++, node = node.parentElement) {
                const roleLabels = Array.from(node.querySelectorAll('h4.sr-only'))
                  .filter((item) => roleLabelRe.test(String(item.textContent || '').trim()));
                if (roleLabels.length > 1) break;
                best = node;
              }
              return best || el.parentElement || el;
            });
          const pairedAssistantTurns = () => {
            const turns = [];
            for (const label of allRoleLabels.filter((el) => userLabelRe.test(String(el.textContent || '').trim()))) {
              let child = label.parentElement;
              for (let i = 0; child && child.parentElement && i < 8; i++, child = child.parentElement) {
                const siblings = Array.from(child.parentElement.children || []);
                const index = siblings.indexOf(child);
                if (index < 0) continue;
                const candidate = siblings.slice(index + 1).find((node) => {
                  const hasUserLabel = Array.from(node.querySelectorAll('h4.sr-only'))
                    .some((item) => userLabelRe.test(String(item.textContent || '').trim()));
                  return !hasUserLabel && Boolean(node.querySelector('[class*="MarkdownRoot"], [data-streaming-response-status]'));
                });
                if (candidate) {
                  if (!turns.includes(candidate)) turns.push(candidate);
                  break;
                }
              }
            }
            return turns;
          };
          const assistantSections = Array.from(document.querySelectorAll('article[data-turn="assistant"], section[data-turn="assistant"]'));
          const assistantRoleNodes = Array.from(document.querySelectorAll('[data-message-author-role="assistant"]'));
          const assistantNodes = assistantSections.length ? assistantSections : (assistantRoleNodes.length ? assistantRoleNodes : (assistantLabelNodes.length ? assistantLabelNodes : pairedAssistantTurns()));
          const lastAssistant = assistantNodes.length ? assistantNodes[assistantNodes.length - 1] : null;
          const streamingNode = lastAssistant ? lastAssistant.querySelector('[data-streaming-response-status]') : null;
          const assistantContent = lastAssistant
            ? (lastAssistant.querySelector('[data-message-author-role="assistant"] .markdown, [data-message-author-role="assistant"]') || lastAssistant.querySelector('.markdown') || lastAssistant)
            : null;
          const lastAssistantText = assistantContent
            ? String((streamingNode ? streamingNode.innerText || streamingNode.textContent : assistantContent.innerText || assistantContent.textContent) || '').trim().slice(-24000)
            : '';
          if (!stop) return JSON.stringify({stopped:false, reason:'not_running', last_assistant_text:lastAssistantText});
          stop.click();
          return JSON.stringify({stopped:true, last_assistant_text:lastAssistantText});
        })()'''
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True},
            timeout_s=1.0,
        )
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("stop_response_invalid") from exc
        if not isinstance(state, dict):
            raise CdpError("stop_response_invalid")
        return state

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
        context_url = _safe_chatgpt_conversation_context_url(conversation_url)
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("human_input_target_invalid")
        try:
            rate_limit_hold_ms = int(os.getenv("BOTTAZZI_GPT_RATE_LIMIT_HOLD_MS", "300000"))
        except ValueError:
            rate_limit_hold_ms = 300_000
        rate_limit_hold_ms = max(60_000, min(1_800_000, rate_limit_hold_ms))
        config = json.dumps(
            {
                "conversation_url": normalized,
                "context_url": context_url,
                "rate_limit_hold_ms": rate_limit_hold_ms,
            },
            ensure_ascii=False,
        )
        expression = r'''(() => {
          const config = __CONFIG__;
          const relayState = window.__bottazziHumanRelayV1;
          if (relayState && relayState.observer) relayState.observer.disconnect();
          try { delete window.__bottazziHumanRelayV1; } catch (_) { window.__bottazziHumanRelayV1 = null; }
          const oldState = window.__bottazziHumanInputTargetV2;
          if (oldState && oldState.observer) oldState.observer.disconnect();
          if (oldState && oldState.onStorage) window.removeEventListener('storage', oldState.onStorage);
          if (oldState && oldState.onFocus) window.removeEventListener('focus', oldState.onFocus);
          try { delete window.__bottazziHumanInputTargetV2; } catch (_) { window.__bottazziHumanInputTargetV2 = null; }
          const priorState = window.__bottazziHumanInputTargetV3;
          if (priorState && priorState.observer) priorState.observer.disconnect();
          if (priorState && priorState.observerTimer) clearTimeout(priorState.observerTimer);
          if (priorState && priorState.onStorage) window.removeEventListener('storage', priorState.onStorage);
          if (priorState && priorState.onFocus) window.removeEventListener('focus', priorState.onFocus);
          try { delete window.__bottazziHumanInputTargetV3; } catch (_) { window.__bottazziHumanInputTargetV3 = null; }
          const stateKey = '__bottazziHumanInputTargetV3';
          const draftKey = `__bottazziHumanDraftV3:${config.conversation_url}`;
          const activeKey = '__bottazziActiveConversationV3';
          const legacyDraftKey = '__bottazziHumanDraftV2';
          try {
            const legacyRaw = localStorage.getItem(legacyDraftKey);
            if (legacyRaw) {
              let legacy = null;
              try { legacy = JSON.parse(legacyRaw); } catch (_) {}
              const createdAt = Number((legacy || {}).created_at || 0);
              if (!createdAt || Date.now() - createdAt > 300000) localStorage.removeItem(legacyDraftKey);
            }
          } catch (_) {}
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
          const rateLimitHoldMs = Math.max(60000, Number(config.rate_limit_hold_ms || 300000));
          const updateHumanUi = () => {
            const box = document.getElementById(humanBoxId);
            const send = document.getElementById(sendId);
            const status = document.getElementById(statusId);
            const panel = document.getElementById(panelId);
            let held = false;
            try { held = Boolean(localStorage.getItem('__bottazziQueueHoldV1')); } catch (_) {}
            const pending = hasPendingDraft();
            const assigned = config.conversation_url;
            const current = canonical(location.href);
            const mismatch = !assigned || current !== assigned;
            const disabled = pending || mismatch;
            if (box && box.disabled !== disabled) box.disabled = disabled;
            if (send && send.disabled !== disabled) send.disabled = disabled;
            const shortId = String(assigned || '').split('/').pop().slice(0, 8) || '—';
            const label = mismatch
              ? `Chat non assegnata · ${shortId}`
              : held || pending
                ? `Siamo in fila · ${shortId}`
                : `Invio umano → ${shortId}`;
            if (status && status.textContent !== label) status.textContent = label;
            if (panel && panel.dataset.bottazziMode === 'active') {
              panel.style.background = mismatch ? '#7a1f1f' : held ? '#6b5200' : 'rgba(30,30,30,.96)';
            }
          };
          const importDraft = () => {
            const limitRe = /(?:temporarily limited access to (?:your )?conversations|temporaneamente (?:limitato )?l['’]?accesso alle conversazioni|attendere qualche minuto prima di riprovare|wait a few minutes before trying again)/i;
            try {
              const holdKey = '__bottazziQueueHoldV1';
              const limited = limitRe.test(String(document.body ? document.body.innerText || '' : ''));
              const previousHold = String(localStorage.getItem(holdKey) || '');
              const now = Date.now();
              if (limited) {
                localStorage.setItem(holdKey, `rate:${Date.now()}`);
              } else if (previousHold.startsWith('rate:')) {
                const heldAt = Number(previousHold.slice(5));
                if (!Number.isFinite(heldAt) || heldAt <= 0 || now - heldAt >= rateLimitHoldMs) {
                  localStorage.removeItem(holdKey);
                }
              } else if (/^\d+$/.test(previousHold)) {
                const heldAt = Number(previousHold);
                if (!Number.isFinite(heldAt) || heldAt <= 0 || now - heldAt >= rateLimitHoldMs) {
                  localStorage.removeItem(holdKey);
                }
              }
              if (localStorage.getItem(holdKey)) { updateHumanUi(); return false; }
            } catch (_) {}
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
            if (!document.body) return;
            let existing = document.getElementById(panelId);
            if (existing && existing.dataset.bottazziMode !== 'active') existing.remove();
            if (document.getElementById(panelId)) return;
            const panel = document.createElement('div');
            panel.id = panelId;
            panel.dataset.bottazziMode = 'active';
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
              if (canonical(location.href) !== config.conversation_url) {
                updateHumanUi();
                return;
              }
              try {
                localStorage.setItem(draftKey, JSON.stringify({draft_id:`${Date.now()}-${Math.random()}`, successor_url:config.context_url, text, submit:true, created_at:Date.now()}));
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
          try { localStorage.setItem(activeKey, config.context_url); } catch (_) {}
          let state = window[stateKey];
          if (!state || typeof state !== 'object') state = {};
          if (state.observer) state.observer.disconnect();
          if (state.observerTimer) clearTimeout(state.observerTimer);
          if (state.onStorage) window.removeEventListener('storage', state.onStorage);
          if (state.onFocus) window.removeEventListener('focus', state.onFocus);
          state.version = 4;
          state.onStorage = event => { if (event.key === draftKey) setTimeout(importDraft, 0); };
          state.onFocus = () => setTimeout(importDraft, 0);
          window.addEventListener('storage', state.onStorage);
          window.addEventListener('focus', state.onFocus);
          state.observerTimer = null;
          state.observer = new MutationObserver(() => {
            if (state.observerTimer) return;
            state.observerTimer = setTimeout(() => {
              state.observerTimer = null;
              lockNative();
              importDraft();
            }, 250);
          });
          state.observer.observe(document.documentElement, {subtree:true, childList:true});
          state.conversation_url = config.conversation_url;
          state.importDraft = importDraft;
          state.updateHumanUi = updateHumanUi;
          window[stateKey] = state;
          lockNative();
          importDraft();
          return JSON.stringify({ok:true, human_input_target:true, conversation_url:config.conversation_url, window_name:window.name, human_composer:Boolean(document.getElementById(humanBoxId)), native_locked:Boolean(findComposer())});
        })()'''.replace("__CONFIG__", config)
        result: dict[str, Any] | None = None
        for attempt in range(3):
            try:
                current = self._wait_target(target_id)
                if not current.websocket_url or not current.is_chatgpt:
                    raise CdpError("human_input_target_invalid")
                result = self._page_call(
                    current.websocket_url,
                    "Runtime.evaluate",
                    {"expression": expression, "returnByValue": True},
                    timeout_s=max(self.timeout_s, 8.0),
                )
                break
            except CdpError as exc:
                transient = (
                    "cdp_timeout:Runtime.evaluate" in str(exc)
                    or "cdp_transport_error:Runtime.evaluate:WebSocketTimeoutException" in str(exc)
                )
                if not transient or attempt >= 2:
                    raise
                time.sleep(0.4 * (attempt + 1))
        if result is None:
            raise CdpError("human_input_target_install_failed")
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("human_input_target_install_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError("human_input_target_install_failed")
        return state

    def set_human_queue_hold(self, target_id: str, held: bool) -> dict[str, Any]:
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("human_queue_target_invalid")
        config = json.dumps({"held": bool(held)})
        expression = r'''(() => {
          const config = __CONFIG__;
          const key = '__bottazziQueueHoldV1';
          try {
            if (config.held) localStorage.setItem(key, `rate:${Date.now()}`);
            else localStorage.removeItem(key);
          } catch (_) {}
          const state = window.__bottazziHumanInputTargetV3 || window.__bottazziHumanInputTargetV2;
          if (state && typeof state.updateHumanUi === 'function') state.updateHumanUi();
          if (!config.held && state && typeof state.importDraft === 'function') setTimeout(state.importDraft, 0);
          return JSON.stringify({ok:true, held:Boolean(config.held)});
        })()'''.replace("__CONFIG__", config)
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("human_queue_state_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError("human_queue_state_failed")
        return state

    def queue_human_message(self, target_id: str, conversation_url: str, text: str) -> dict[str, Any]:
        normalized = _canonical_chatgpt_conversation_url(conversation_url)
        if not normalized or not text.strip():
            raise CdpError("human_queue_message_invalid")
        context_url = _safe_chatgpt_conversation_context_url(conversation_url)
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("human_queue_target_invalid")
        if _canonical_chatgpt_conversation_url(target.url) != normalized:
            raise CdpError("human_queue_assignment_mismatch")
        config = json.dumps({"conversation_url": normalized, "context_url": context_url, "text": text.strip()}, ensure_ascii=False)
        expression = r'''(() => {
          const config = __CONFIG__;
          const draftKey = `__bottazziHumanDraftV3:${config.conversation_url}`;
          const activeKey = '__bottazziActiveConversationV3';
          let existing = null;
          try { existing = localStorage.getItem(draftKey); } catch (_) {}
          if (existing) return JSON.stringify({ok:true, queued:false, reason:'queue_busy'});
          try {
            localStorage.setItem(activeKey, config.context_url);
            localStorage.setItem(draftKey, JSON.stringify({
              draft_id:`${Date.now()}-${Math.random()}`,
              successor_url:config.context_url,
              text:config.text,
              submit:true,
              created_at:Date.now(),
            }));
          } catch (_) { return JSON.stringify({ok:false, reason:'queue_storage_failed'}); }
          const state = window.__bottazziHumanInputTargetV3 || window.__bottazziHumanInputTargetV2;
          if (state && typeof state.updateHumanUi === 'function') state.updateHumanUi();
          if (state && typeof state.importDraft === 'function') setTimeout(state.importDraft, 0);
          return JSON.stringify({ok:true, queued:true});
        })()'''.replace("__CONFIG__", config)
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("human_queue_message_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError(str(state.get("reason") or "human_queue_message_failed"))
        return state

    def attach_chatgpt_file(
        self,
        target_id: str,
        conversation_url: str,
        file_path: str | os.PathLike[str],
        *,
        image_only: bool = False,
    ) -> dict[str, Any]:
        normalized = _canonical_chatgpt_conversation_url(conversation_url)
        if not normalized:
            raise CdpError("attachment_conversation_url_invalid")
        path = Path(file_path).expanduser().resolve()
        if not path.is_file():
            raise CdpError("attachment_file_missing")
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("attachment_target_invalid")
        if _canonical_chatgpt_conversation_url(target.url) != normalized:
            raise CdpError("attachment_assignment_mismatch")
        expression = (
            "Array.from(document.querySelectorAll('input[type=file]')).find(x=>"
            + (
                "x.getAttribute('data-testid')==='upload-photos-input'||String(x.accept||'').includes('image')"
                if image_only
                else "!String(x.accept||'').trim()"
            )
            + ")||null"
        )
        result = self._page_call(
            target.websocket_url,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": False},
        )
        object_id = (result.get("result") or {}).get("objectId")
        if not object_id:
            raise CdpError("attachment_input_not_found")
        self._page_call(
            target.websocket_url,
            "DOM.setFileInputFiles",
            {"objectId": object_id, "files": [str(path)]},
            timeout_s=20.0,
        )
        basename = path.name
        name_json = json.dumps(basename)
        deadline = time.monotonic() + 12.0
        last_state: dict[str, Any] = {}
        while time.monotonic() < deadline:
            verify_expression = (
                f"(() => {{ const name={name_json}; "
                "const inputs=[...document.querySelectorAll('input[type=file]')]; "
                "const inInput=inputs.some(x=>[...x.files].some(f=>f.name===name)); "
                "const body=String(document.body?.innerText||''); "
                "return JSON.stringify({ok:inInput||body.includes(name),inInput,seen:body.includes(name)}); }})()"
            )
            verify = self._page_call(
                target.websocket_url,
                "Runtime.evaluate",
                {"expression": verify_expression, "returnByValue": True},
            )
            raw = (verify.get("result") or {}).get("value")
            try:
                last_state = json.loads(raw) if isinstance(raw, str) else {}
            except json.JSONDecodeError:
                last_state = {}
            if isinstance(last_state, dict) and last_state.get("ok"):
                return {"attached": True, "filename": basename, "image_only": bool(image_only)}
            time.sleep(0.25)
        raise CdpError(f"attachment_not_confirmed:{json.dumps(last_state, sort_keys=True)}")

    def install_tab_identity(self, target_id: str, *, mode: str = "other", countdown_seconds: int | None = None) -> dict[str, Any]:
        if mode not in {"active", "other", "queue", "closing"}:
            raise CdpError("tab_identity_mode_invalid")
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("tab_identity_target_invalid")
        config = json.dumps({"mode": mode, "countdown_seconds": countdown_seconds}, ensure_ascii=False)
        expression = r'''(() => {
          const config = __CONFIG__;
          for (const legacyKey of ['__bottazziTabIdentityV1','__bottazziTabIdentityV2']) {
            const legacy = window[legacyKey];
            if (legacy && legacy.timer) clearInterval(legacy.timer);
            try { delete window[legacyKey]; } catch (_) { window[legacyKey] = null; }
          }
          const key = '__bottazziTabIdentityV3';
          let state = window[key];
          if (!state || typeof state !== 'object') state = {base_title:'', original_title:'', timer:null, close_at:0};
          const canonical = value => {
            try {
              const u = new URL(String(value || ''), location.origin);
              const m = u.pathname.match(/^\/(?:g\/[^/]+\/)?c\/([A-Za-z0-9-]+)/);
              return m ? `${u.origin}/c/${m[1]}` : '';
            } catch (_) { return ''; }
          };
          const strip = value => String(value || '')
            .replace(/^(?:🟢(?:\s*ATTIVA)?|⚪(?:\s*ALTRA)?|🟡(?:\s*IN FILA)?|🟠(?:\s*CHIUSURA)?(?:\s*\d+s)?|🔴(?:\s*CHIUSURA)?(?:\s*\d+s)?|📁|➕)\s*(?:·\s*)?/iu, '')
            .replace(/^ChatGPT\s*-\s*/i, '').trim();
          const compactWord = word => {
            const value = String(word || '');
            return value.length > 11 ? `${value.slice(0, 10)}…` : value;
          };
          const compactTopic = value => {
            const stop = new Set(['riprendi','lavoro','verifica','aggiorna','creare','definire','test','meccanismo','matematico','problema','accesso','stato','nuovo','nuova','chat','il','lo','la','i','gli','le','di','del','della','dei','degli','delle','in','su','per','a','al','alla','ai','alle','da']);
            const words = strip(value).split(/\s+/).filter(Boolean);
            const useful = words.filter(word => !stop.has(word.toLowerCase())).slice(0, 2).map(compactWord);
            const chosen = useful.length ? useful : words.slice(-2).map(compactWord);
            let text = chosen.join(' ');
            if (text.length > 22) text = `${text.slice(0, 21).trimEnd()}…`;
            return text;
          };
          const compactProject = value => {
            const stop = new Set(['progetto','indipendentemenza','dai','da','di','del','della','dei','degli','delle']);
            const words = strip(value).split(/\s+/).filter(Boolean).filter(word => !stop.has(word.toLowerCase()));
            const chosen = (words.length > 2 ? words.slice(-2) : words).map(compactWord);
            let text = chosen.join(' ');
            if (text.length > 22) text = `${text.slice(0, 21).trimEnd()}…`;
            return text;
          };
          const topicFromSidebar = () => {
            const wanted = canonical(location.href);
            if (!wanted) return '';
            for (const a of document.querySelectorAll('a[href]')) {
              const rawHref = String(a.getAttribute('href') || '').trim();
              if (!rawHref || rawHref.startsWith('#') || rawHref.startsWith('javascript:')) continue;
              if (canonical(rawHref) !== wanted) continue;
              const text = strip(a.innerText || a.textContent || '');
              if (!text || text.length > 180) continue;
              if (/^(?:vai ai contenuti|skip to content|main content|chatgpt|new chat)$/i.test(text)) continue;
              return text;
            }
            return '';
          };
          if (!state.original_title) state.original_title = strip(document.title || '');
          const projectFromPath = () => {
            const match = location.pathname.match(/^\/g\/([^/]+)(?:\/c\/|\/project|$)/);
            if (!match) return '';
            let slug = '';
            try { slug = decodeURIComponent(match[1]); } catch (_) { slug = match[1]; }
            slug = slug.replace(/^g-p-[A-Za-z0-9]+-?/i, '').replace(/[-_]+/g, ' ').trim();
            return compactProject(slug);
          };
          const derive = () => {
            const topic = compactTopic(topicFromSidebar());
            if (topic) return topic;
            if (location.pathname === '/' || location.pathname === '') return 'Nuova';
            const project = projectFromPath();
            if (/\/project\/?$/.test(location.pathname) && project) return project;
            const fallback = compactTopic(state.original_title || '');
            return fallback || project || 'Chat';
          };
          if (!state.base_title) state.base_title = derive();
          state.mode = config.mode;
          if (config.mode === 'closing' && Number(config.countdown_seconds) > 0 && !state.close_at) {
            state.close_at = Date.now() + Number(config.countdown_seconds) * 1000;
          }
          if (config.mode !== 'closing') state.close_at = 0;
          const render = () => {
            const found = derive();
            if (found && found !== 'Chat') state.base_title = found;
            const isConversation = Boolean(canonical(location.href));
            let prefix = !isConversation
              ? (/\/project\/?$/.test(location.pathname) ? '📁' : '➕')
              : config.mode === 'active' ? '🟢'
              : config.mode === 'queue' ? '🟡'
              : config.mode === 'closing' ? '🟠'
              : '⚪';
            if (isConversation && config.mode === 'closing' && state.close_at) {
              const remaining = Math.max(0, Math.ceil((state.close_at - Date.now()) / 1000));
              prefix = remaining <= 5 ? `🔴${remaining}s` : `🟠${remaining}s`;
            }
            document.title = `${prefix} ${state.base_title}`;
          };
          if (state.timer) clearInterval(state.timer);
          state.timer = setInterval(render, 750);
          window[key] = state;
          render();
          return JSON.stringify({ok:true, mode:config.mode, base_title:state.base_title, title:document.title});
        })()'''.replace("__CONFIG__", config)
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("tab_identity_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError("tab_identity_failed")
        return state

    def install_human_input_relay(self, target_id: str, active_conversation_url: str) -> dict[str, Any]:
        active_identity = _canonical_chatgpt_conversation_url(active_conversation_url)
        if not active_identity:
            raise CdpError("human_input_relay_url_invalid")
        active_url = _safe_chatgpt_conversation_context_url(active_conversation_url)
        target = self._wait_target(target_id)
        if not target.websocket_url or not target.is_chatgpt:
            raise CdpError("human_input_relay_target_invalid")
        config = json.dumps({"active_url": active_url, "active_identity": active_identity}, ensure_ascii=False)
        expression = r'''(() => {
          const config = __CONFIG__;
          if (window.__bottazziGhostTabV1) return JSON.stringify({ok:true, relay:false, ghost:true});
          const panelId = 'bottazzi-human-panel';
          const boxId = 'bottazzi-human-composer';
          const statusId = 'bottazzi-human-status';
          const draftKey = '__bottazziHumanDraftV2';
          const activeKey = '__bottazziActiveConversationV2';
          const relayKey = '__bottazziHumanRelayV1';
          const activeStateV2 = window.__bottazziHumanInputTargetV2;
          if (activeStateV2 && activeStateV2.observer) activeStateV2.observer.disconnect();
          if (activeStateV2 && activeStateV2.onStorage) window.removeEventListener('storage', activeStateV2.onStorage);
          if (activeStateV2 && activeStateV2.onFocus) window.removeEventListener('focus', activeStateV2.onFocus);
          try { delete window.__bottazziHumanInputTargetV2; } catch (_) { window.__bottazziHumanInputTargetV2 = null; }
          const activeStateV3 = window.__bottazziHumanInputTargetV3;
          if (activeStateV3 && activeStateV3.observer) activeStateV3.observer.disconnect();
          if (activeStateV3 && activeStateV3.observerTimer) clearTimeout(activeStateV3.observerTimer);
          if (activeStateV3 && activeStateV3.onStorage) window.removeEventListener('storage', activeStateV3.onStorage);
          if (activeStateV3 && activeStateV3.onFocus) window.removeEventListener('focus', activeStateV3.onFocus);
          try { delete window.__bottazziHumanInputTargetV3; } catch (_) { window.__bottazziHumanInputTargetV3 = null; }
          if (window.name === 'bottazzi-active') window.name = '';
          let panel = document.getElementById(panelId);
          if (panel && panel.dataset.bottazziMode !== 'relay') panel.remove();
          panel = document.getElementById(panelId);
          if (!panel && document.body) {
            panel = document.createElement('div');
            panel.id = panelId;
            panel.dataset.bottazziMode = 'relay';
            panel.style.cssText = 'position:fixed;left:50%;bottom:12px;transform:translateX(-50%);z-index:2147483646;width:min(760px,calc(100vw - 32px));padding:8px 10px;border-radius:14px;background:rgba(30,30,30,.96);color:#fff;font:600 13px/1.3 system-ui,sans-serif;box-shadow:0 5px 22px rgba(0,0,0,.32)';
            const status = document.createElement('div');
            status.id = statusId;
            status.textContent = 'Invio umano → 🟢 chat attiva';
            status.style.cssText = 'margin:0 2px 6px;opacity:.8;font-size:12px';
            const row = document.createElement('div');
            row.style.cssText = 'display:flex;gap:8px;align-items:flex-end';
            const input = document.createElement('textarea');
            input.id = boxId;
            input.rows = 2;
            input.placeholder = 'Scrivi a Bot-tazzi…';
            input.style.cssText = 'flex:1;resize:vertical;max-height:180px;padding:9px 10px;border-radius:10px;border:1px solid rgba(255,255,255,.25);background:#fff;color:#111;font:14px/1.35 system-ui,sans-serif';
            const send = document.createElement('button');
            send.type = 'button';
            send.textContent = 'Invia';
            send.style.cssText = 'padding:10px 14px;border-radius:10px;border:0;cursor:pointer;font-weight:800';
            const forward = () => {
              const text = input.value.trim();
              if (!text) return;
              try {
                localStorage.setItem(activeKey, config.active_url);
                localStorage.setItem(draftKey, JSON.stringify({draft_id:`${Date.now()}-${Math.random()}`, successor_url:config.active_url, text, submit:true, created_at:Date.now()}));
              } catch (_) { return; }
              input.value = '';
              status.textContent = 'Siamo in fila · attendi';
            };
            send.addEventListener('click', forward);
            input.addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); forward(); } });
            row.appendChild(input);
            row.appendChild(send);
            panel.appendChild(status);
            panel.appendChild(row);
            document.body.appendChild(panel);
          }
          let relayState = window[relayKey];
          if (!relayState || typeof relayState !== 'object') relayState = {};
          if (relayState.observer) relayState.observer.disconnect();
          relayState.observer = null;
          window[relayKey] = relayState;
          try { localStorage.setItem(activeKey, config.active_url); } catch (_) {}
          return JSON.stringify({ok:true, relay:true, active_url:config.active_url, human_composer:Boolean(document.getElementById(boxId))});
        })()'''.replace("__CONFIG__", config)
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("human_input_relay_invalid") from exc
        if not isinstance(state, dict) or not state.get("ok"):
            raise CdpError("human_input_relay_failed")
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
        successor = None
        if successor_url:
            successor = _safe_chatgpt_conversation_context_url(successor_url)
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
          const activeStateV2 = window.__bottazziHumanInputTargetV2;
          if (activeStateV2 && activeStateV2.observer) activeStateV2.observer.disconnect();
          if (activeStateV2 && activeStateV2.onStorage) window.removeEventListener('storage', activeStateV2.onStorage);
          if (activeStateV2 && activeStateV2.onFocus) window.removeEventListener('focus', activeStateV2.onFocus);
          try { delete window.__bottazziHumanInputTargetV2; } catch (_) { window.__bottazziHumanInputTargetV2 = null; }
          const activeStateV3 = window.__bottazziHumanInputTargetV3;
          if (activeStateV3 && activeStateV3.observer) activeStateV3.observer.disconnect();
          if (activeStateV3 && activeStateV3.observerTimer) clearTimeout(activeStateV3.observerTimer);
          if (activeStateV3 && activeStateV3.onStorage) window.removeEventListener('storage', activeStateV3.onStorage);
          if (activeStateV3 && activeStateV3.onFocus) window.removeEventListener('focus', activeStateV3.onFocus);
          try { delete window.__bottazziHumanInputTargetV3; } catch (_) { window.__bottazziHumanInputTargetV3 = null; }
          const activePanel = document.getElementById('bottazzi-human-panel');
          if (activePanel) activePanel.remove();
          window.name = '';
          const stateKey = '__bottazziGhostTabV1';
          const bannerId = 'bottazzi-human-panel';
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
            banner.style.cssText = 'position:fixed;left:50%;bottom:12px;transform:translateX(-50%);z-index:2147483647;width:min(760px,calc(100vw - 32px));padding:10px 12px;border-radius:14px;background:#7a4b00;color:#fff;font:600 14px/1.35 system-ui,sans-serif;box-shadow:0 5px 22px rgba(0,0,0,.32);transition:background .2s ease,box-shadow .2s ease';
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
            const panel = document.getElementById(bannerId);
            const countdown = document.getElementById(countdownId);
            if (!countdown || !panel) return;
            const remaining = Math.max(0, Math.ceil((Number(state.close_at || 0) - Date.now()) / 1000));
            countdown.textContent = remaining > 0 ? `CHAT IN CHIUSURA · ${remaining} s` : 'CHIUSURA…';
            const background = remaining <= 5 ? '#b91c1c' : remaining <= 10 ? '#9a3412' : '#7a4b00';
            panel.style.background = background;
            panel.style.boxShadow = remaining <= 5 ? '0 0 0 3px rgba(255,255,255,.28),0 7px 28px rgba(0,0,0,.45)' : '0 5px 22px rgba(0,0,0,.32)';
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

    def kimi_ui_state(self, target_id: str) -> dict[str, Any]:
        target = self._wait_target(target_id)
        try:
            host = (urllib.parse.urlparse(target.url).hostname or "").lower()
        except ValueError as exc:
            raise CdpError("kimi_target_invalid") from exc
        if host not in {"kimi.com", "www.kimi.com", "www.kimi.ai", "kimi.ai"} or not target.websocket_url:
            raise CdpError("kimi_target_invalid")
        expression = r"""(() => {
          const visible = el => {
            if (!el) return false;
            const r=el.getBoundingClientRect(), s=getComputedStyle(el);
            return r.width>0 && r.height>0 && s.display!=='none' && s.visibility!=='hidden';
          };
          const composerSelectors = [
            '[contenteditable="true"][role="textbox"]',
            '.ProseMirror[contenteditable="true"]',
            'textarea'
          ];
          let composer=null;
          for (const selector of composerSelectors) {
            composer=[...document.querySelectorAll(selector)].find(visible)||null;
            if (composer) break;
          }
          const textOf = el => String(el?.innerText || el?.textContent || '').replace(/\s+/g,' ').trim();
          const candidates=[];
          const finalSelectors=[
            '.chat-content-item-assistant .markdown-container:not(.toolcall-content-text)',
            '.segment-assistant .markdown-container:not(.toolcall-content-text)',
            '[data-role="assistant"] .markdown-container:not(.toolcall-content-text)',
            '[data-message-role="assistant"] .markdown-container:not(.toolcall-content-text)'
          ];
          const seen=new Set();
          for (const selector of finalSelectors) for (const el of document.querySelectorAll(selector)) {
            if (!visible(el) || el===composer || el.contains(composer) || composer?.contains(el)) continue;
            if (el.closest('.thinking-container,.toolcall-content,.toolcall-flow__body')) continue;
            const text=textOf(el);
            if (!text || text.length<1 || text.length>120000 || seen.has(text)) continue;
            seen.add(text); candidates.push(text);
          }
          const stopRe=/(?:stop|停止|interrompi|annulla|cancel)/i;
          const responseInProgress=[...document.querySelectorAll('button')].some(b=>visible(b)&&stopRe.test(`${b.getAttribute('aria-label')||''} ${b.getAttribute('title')||''} ${textOf(b)}`));
          const bodyText=String(document.body?.innerText||'');
          const loginVisible=[...document.querySelectorAll('button,a')].some(el=>visible(el)&&/^(?:login|log in|sign in|accedi|登录)$/i.test(textOf(el))) || /(?:微信扫码登录|手机号码登录|发送验证码|登录以同步历史会话|sign in|log in|accedi)/i.test(bodyText);
          const challengeVisible=/(?:captcha|verify you are human|robot|机器人|人机验证|安全验证|verification)/i.test(bodyText);
          const composerText=composer ? String(composer.value ?? composer.innerText ?? composer.textContent ?? '').trim() : '';
          return JSON.stringify({
            ready:Boolean(composer), url:location.href, title:document.title||'',
            composer_ready:Boolean(composer), composer_chars:composerText.length,
            response_in_progress:responseInProgress, login_visible:loginVisible, challenge_visible:challengeVisible,
            candidate_count:candidates.length, last_assistant_text:candidates.length?candidates[candidates.length-1]:'',
            body_text:bodyText.slice(-16000)
          });
        })()"""
        result = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": expression, "returnByValue": True})
        raw = (result.get("result") or {}).get("value")
        try:
            state = json.loads(raw) if isinstance(raw, str) else {}
        except json.JSONDecodeError as exc:
            raise CdpError("kimi_ui_state_invalid") from exc
        if not isinstance(state, dict):
            raise CdpError("kimi_ui_state_invalid")
        state["target_id"] = target.target_id
        return state

    def query_kimi(self, target_id: str, prompt: str, *, wait_timeout_s: float = 90.0) -> dict[str, Any]:
        clean = str(prompt or "").strip()
        if not clean:
            raise CdpError("empty_prompt")
        baseline = self.kimi_ui_state(target_id)
        if not baseline.get("composer_ready"):
            raise CdpError("kimi_not_ready:composer_missing")
        target = self._wait_target(target_id)
        if not target.websocket_url:
            raise CdpError("kimi_target_missing_websocket")
        focus = r"""(() => {
          const visible=el=>{if(!el)return false;const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'};
          const selectors=['[contenteditable="true"][role="textbox"]','.ProseMirror[contenteditable="true"]','textarea'];
          let el=null;for(const selector of selectors){el=[...document.querySelectorAll(selector)].find(visible)||null;if(el)break}
          if(!el)return JSON.stringify({ok:false,reason:'composer_not_found'});
          el.focus();
          if(el instanceof HTMLTextAreaElement||el instanceof HTMLInputElement)el.select();
          else{const sel=getSelection(),range=document.createRange();range.selectNodeContents(el);sel.removeAllRanges();sel.addRange(range)}
          return JSON.stringify({ok:true});
        })()"""
        focused = self._page_call(target.websocket_url, "Runtime.evaluate", {"expression": focus, "returnByValue": True})
        try:
            focus_state=json.loads((focused.get("result") or {}).get("value") or "{}")
        except json.JSONDecodeError:
            focus_state={}
        if not focus_state.get("ok"):
            raise CdpError("kimi_prompt_focus_failed")
        self._page_call(target.websocket_url, "Input.insertText", {"text": clean})
        common={"key":"Enter","code":"Enter","windowsVirtualKeyCode":13,"nativeVirtualKeyCode":13}
        self._page_call(target.websocket_url, "Input.dispatchKeyEvent", {"type":"keyDown", **common})
        self._page_call(target.websocket_url, "Input.dispatchKeyEvent", {"type":"keyUp", **common})
        deadline=time.monotonic()+max(5.0,float(wait_timeout_s))
        baseline_text=str(baseline.get("last_assistant_text") or "")
        seen_generation=False
        stable_text=""; stable_since=0.0
        while time.monotonic()<deadline:
            time.sleep(0.35)
            state=self.kimi_ui_state(target_id)
            if state.get("login_visible") or state.get("challenge_visible"):
                reason = "challenge_required" if state.get("challenge_visible") else "login_required"
                raise CdpError(f"kimi_query_failed:{reason}")
            current=str(state.get("last_assistant_text") or "").strip()
            if state.get("response_in_progress"):
                seen_generation=True
            changed=bool(current and current!=baseline_text and current!=clean)
            if changed:
                if current!=stable_text:
                    stable_text=current; stable_since=time.monotonic()
                elif not state.get("response_in_progress") and time.monotonic()-stable_since>=1.0:
                    return {"provider":"kimi","target_id":target_id,"response":current,"url":state.get("url"),"login_visible":bool(state.get("login_visible"))}
            if seen_generation and changed and not state.get("response_in_progress"):
                return {"provider":"kimi","target_id":target_id,"response":current,"url":state.get("url"),"login_visible":bool(state.get("login_visible"))}
        if stable_text:
            return {"provider":"kimi","target_id":target_id,"response":stable_text,"url":self._wait_target(target_id).url,"timed_out":True}
        final=self.kimi_ui_state(target_id)
        reason=("challenge_required" if final.get("challenge_visible") else "login_required" if final.get("login_visible") else "response_timeout")
        raise CdpError(f"kimi_query_failed:{reason}")

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

    def activate_target(self, target_id: str) -> None:
        target = self._wait_target(target_id)
        if target.target_type != "page" or not target.is_chatgpt:
            raise CdpError("activate_target_invalid")
        self._browser_call("Target.activateTarget", {"targetId": target_id})

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

    def _page_call(
        self,
        websocket_url: str,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        return self._rpc(websocket_url, method, params, timeout_s=timeout_s)

    def _rpc(
        self,
        websocket_url: str,
        method: str,
        params: dict[str, Any] | None,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        ws = None
        effective_timeout = self.timeout_s if timeout_s is None else max(0.05, float(timeout_s))
        try:
            ws = websocket.create_connection(
                websocket_url,
                timeout=effective_timeout,
                suppress_origin=True,
            )
            ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
            deadline = time.monotonic() + effective_timeout
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
