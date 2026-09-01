from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO

import requests

from ralfloop_agent.cli.repo_context import RepoContextError, collect_repo_context, resolve_cwd
from ralfloop_agent.cli.session_store import SessionStore, SessionStoreError
from ralfloop_agent.integration.interaction_router import (
    InteractionDecision,
    classify_interaction,
    explicit_agent_decision,
)
from ralfloop_agent.providers.agent_gpu_handoff import AgentGpuCoordinator, AgentGpuHandoffError
from ralfloop_agent.providers.llama_cpp_server import (
    LlamaCppServerError,
    LlamaCppServerConfig,
    LlamaCppServerManager,
)

DEFAULT_BASE_URL = "http://127.0.0.1:19090"
DEFAULT_AGENT_TIMEOUT = 600.0
DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_INACTIVITY_TIMEOUT = 60.0
DEFAULT_HISTORY_LIMIT = 12
DEFAULT_HISTORY_CHARS = 24_000
CHAT_PROVIDERS = ("ollama", "llama_cpp", "remote_tool", "speculative_local", "speculative_remote")
EXPERIMENTAL_PROVIDERS = set(CHAT_PROVIDERS) - {"ollama", "llama_cpp"}

CHAT_ENDPOINT = "/chat"
CHAT_STREAM_ENDPOINT = "/chat/stream"
TASK_ENDPOINT = "/tasks/run"
STREAM_FALLBACK_STATUSES = {404, 405, 501}

TEXT_KEYS = ("answer", "final_answer", "response", "message", "text", "output", "result", "data", "payload")
SECRET_MARKERS = ("secret", "token", "hmac", "key", "authorization", "cookie", "password", "nonce")
ANSI_ESCAPE_RE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[PX^_].*?\x1b\\|[@-_])",
    re.DOTALL,
)
TERMINAL_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
MOJIBAKE_MARKERS = ("Ã", "Â", "â€", "ðŸ")


class RalfTerminalError(Exception):
    """User-facing terminal client error."""


class BackendUnavailable(RalfTerminalError):
    pass


class BackendTimeout(RalfTerminalError):
    pass


class StreamFastPathUnavailable(RalfTerminalError):
    pass


class BackendHTTPError(RalfTerminalError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


@dataclass
class ChatConfig:
    base_url: str = DEFAULT_BASE_URL
    agent_timeout: float = DEFAULT_AGENT_TIMEOUT
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT
    history_limit: int = DEFAULT_HISTORY_LIMIT
    raw: bool = False
    json_output: bool = False
    no_history: bool = False
    stream: bool = True
    continue_session: bool = False
    session_id: str | None = None
    cwd: str | None = None
    model: str | None = None
    provider: str = "llama_cpp"
    timings: bool = False
    last_timings: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ChatConfig":
        return cls(
            base_url=os.getenv("RALF_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            agent_timeout=_env_float("RALF_AGENT_TIMEOUT", DEFAULT_AGENT_TIMEOUT),
            connect_timeout=_env_float("RALF_CHAT_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT),
            inactivity_timeout=_env_float("RALF_CHAT_INACTIVITY_TIMEOUT", DEFAULT_INACTIVITY_TIMEOUT),
            history_limit=max(1, _env_int("RALF_CHAT_HISTORY_LIMIT", DEFAULT_HISTORY_LIMIT)),
            stream=_env_bool("RALF_CHAT_STREAM", True),
            provider=(os.getenv("RALF_CHAT_PROVIDER", "llama_cpp").strip().lower() or "llama_cpp"),
            timings=_env_bool("RALF_CHAT_TIMINGS", False),
        )


@dataclass
class Turn:
    user: str
    ralf: str


@dataclass
class ChatSession:
    history_limit: int = DEFAULT_HISTORY_LIMIT
    session_id: str = ""
    cwd: str = ""
    model: str | None = None
    provider: str | None = None
    context_enabled: bool = True
    created_at: str = ""
    updated_at: str = ""
    turns: list[Turn] = field(default_factory=list)

    def add(self, user: str, ralf: str) -> None:
        self.turns.append(Turn(user=user, ralf=ralf))
        if len(self.turns) > self.history_limit:
            self.turns = self.turns[-self.history_limit :]

    def reset(self) -> None:
        self.turns.clear()

    def compact(self) -> list[dict[str, str]]:
        return [{"user": turn.user, "ralf": turn.ralf} for turn in self.turns]

    def messages(self, max_chars: int = DEFAULT_HISTORY_CHARS) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        remaining = max(0, max_chars)
        for turn in reversed(self.turns[-self.history_limit :]):
            pair = [
                {"role": "user", "content": turn.user},
                {"role": "assistant", "content": turn.ralf},
            ]
            pair_chars = sum(len(item["content"]) for item in pair)
            if pair_chars > remaining:
                if rows or remaining < 2:
                    break
                user_budget = remaining // 2
                assistant_budget = remaining - user_budget
                pair[0]["content"] = pair[0]["content"][-user_budget:]
                pair[1]["content"] = pair[1]["content"][-assistant_budget:]
                rows = pair
                break
            rows[0:0] = pair
            remaining -= pair_chars
        return rows

    def to_record(self) -> dict[str, Any]:
        history: list[dict[str, str]] = []
        for turn in self.turns[-self.history_limit :]:
            history.extend(
                [
                    {"role": "user", "content": turn.user},
                    {"role": "assistant", "content": turn.ralf},
                ]
            )
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "cwd": self.cwd,
            "history": history,
            "model": self.model,
            "context_enabled": self.context_enabled,
            "metadata": {"provider": self.provider} if self.provider else {},
        }

    @classmethod
    def from_record(cls, record: dict[str, Any], *, history_limit: int) -> "ChatSession":
        history = record.get("history") if isinstance(record.get("history"), list) else []
        turns: list[Turn] = []
        pending_user: str | None = None
        for item in history:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = str(item.get("content") or "")
            if role == "user":
                pending_user = content
            elif role == "assistant" and pending_user is not None:
                turns.append(Turn(user=pending_user, ralf=content))
                pending_user = None
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        return cls(
            history_limit=history_limit,
            session_id=str(record.get("session_id") or ""),
            cwd=str(record.get("cwd") or ""),
            model=str(record["model"]) if record.get("model") else None,
            provider=str(metadata["provider"]) if metadata.get("provider") else None,
            context_enabled=bool(record.get("context_enabled", True)),
            created_at=str(record.get("created_at") or ""),
            updated_at=str(record.get("updated_at") or ""),
            turns=turns[-history_limit:],
        )


class RalfHTTPClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        inactivity_timeout: float = DEFAULT_INACTIVITY_TIMEOUT,
        agent_timeout: float = DEFAULT_AGENT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.inactivity_timeout = inactivity_timeout
        self.agent_timeout = agent_timeout
        self.session = session or requests.Session()
        self.last_endpoint: str | None = None

    @property
    def task_url(self) -> str:
        return f"{self.base_url}{TASK_ENDPOINT}"

    def post_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", CHAT_ENDPOINT, payload, timeout=(self.connect_timeout, self.inactivity_timeout))

    def post_chat_stream(self, payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
        self.last_endpoint = CHAT_STREAM_ENDPOINT
        response: requests.Response | None = None
        try:
            try:
                response = self.session.post(
                    f"{self.base_url}{CHAT_STREAM_ENDPOINT}",
                    json=payload,
                    headers={"Accept": "application/x-ndjson"},
                    stream=True,
                    timeout=(self.connect_timeout, self.inactivity_timeout),
                )
            except requests.RequestException as exc:
                raise _transport_error(exc, streaming=True) from exc
            if response.status_code in STREAM_FALLBACK_STATUSES:
                raise StreamFastPathUnavailable(f"stream_fast_path_unavailable:{response.status_code}")
            if response.status_code >= 400:
                raise BackendHTTPError(response.status_code, response.text)
            saw_event = False
            try:
                for raw_line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if not raw_line:
                        continue
                    saw_event = True
                    try:
                        event = json.loads(raw_line)
                    except (TypeError, json.JSONDecodeError) as exc:
                        raise RalfTerminalError("invalid_ndjson") from exc
                    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                        raise RalfTerminalError("invalid_ndjson_event")
                    yield event
            except requests.RequestException as exc:
                raise _transport_error(exc, streaming=True) from exc
            if not saw_event:
                raise RalfTerminalError("empty_stream")
        finally:
            if response is not None:
                response.close()

    def post_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", TASK_ENDPOINT, payload, timeout=(self.connect_timeout, self.agent_timeout))

    def get_json(self, path: str) -> dict[str, Any]:
        return self._request_json("GET", path, None, timeout=(self.connect_timeout, self.inactivity_timeout))

    def _request_json(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        *,
        timeout: tuple[float, float],
    ) -> dict[str, Any]:
        self.last_endpoint = path
        try:
            response = self.session.request(
                method,
                f"{self.base_url}{path}",
                json=payload,
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise _transport_error(exc, streaming=False) from exc
        try:
            if response.status_code >= 400:
                raise BackendHTTPError(response.status_code, response.text)
            try:
                data = response.json()
            except ValueError as exc:
                raise RalfTerminalError("invalid_json") from exc
        finally:
            response.close()
        return data if isinstance(data, dict) else {"payload": data}


def _transport_error(exc: requests.RequestException, *, streaming: bool) -> RalfTerminalError:
    if isinstance(exc, requests.ConnectTimeout):
        return BackendTimeout("backend_connect_timeout")
    if isinstance(exc, (requests.ReadTimeout, requests.Timeout)) or "read timed out" in str(exc).lower():
        return BackendTimeout("stream_inactivity_timeout" if streaming else "backend_inactivity_timeout")
    return BackendUnavailable(f"backend_unreachable: {exc}")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in SECRET_MARKERS):
                out[sanitize_terminal_text(str(key))] = "[REDACTED]"
            else:
                out[sanitize_terminal_text(str(key))] = sanitize_json(item)
        return out
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    if isinstance(value, str):
        return sanitize_terminal_text(value)
    return value


def sanitize_terminal_text(value: str) -> str:
    cleaned = TERMINAL_CONTROL_RE.sub("", ANSI_ESCAPE_RE.sub("", value))
    if any(marker in cleaned for marker in MOJIBAKE_MARKERS):
        try:
            repaired = cleaned.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return cleaned
        if sum(repaired.count(marker) for marker in MOJIBAKE_MARKERS) < sum(
            cleaned.count(marker) for marker in MOJIBAKE_MARKERS
        ):
            return repaired
    return cleaned


def configure_standard_streams_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass


def extract_text_response(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        for key in TEXT_KEYS:
            if key not in payload:
                continue
            value = payload[key]
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, dict):
                nested = extract_text_response(value)
                if nested:
                    return nested
        envelope = payload.get("result_envelope")
        if isinstance(envelope, dict):
            nested = extract_text_response(envelope)
            if nested:
                return nested
        return json.dumps(sanitize_json(payload), ensure_ascii=False, indent=2, sort_keys=True)
    return json.dumps(sanitize_json(payload), ensure_ascii=False, indent=2, sort_keys=True)


def find_approval_request(payload: Any) -> dict[str, str] | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("pending_confirmation_id"):
        return {
            "action": "external_action",
            "request_id": str(payload["pending_confirmation_id"]),
            "digest": "",
            "status": str(payload.get("stop_reason") or "pending"),
        }
    for key in ("approval", "approval_request", "domain_approval", "telegram_approval", "human_approval", "request"):
        value = payload.get(key)
        found = _approval_from_dict(value) if isinstance(value, dict) else None
        if found:
            return found
    return _approval_from_dict(payload)


def _approval_from_dict(payload: dict[str, Any]) -> dict[str, str] | None:
    request_id = payload.get("approval_request_id") or payload.get("request_id")
    status = str(payload.get("status") or "").lower()
    digest = payload.get("scope_digest_short") or payload.get("scope_digest") or payload.get("digest")
    action = payload.get("approval_action") or payload.get("action")
    if not request_id or not (digest or action or status in {"pending", "requested", "human_confirmation_required"}):
        return None
    return {
        "action": str(action or ""),
        "request_id": str(request_id),
        "digest": str(digest or ""),
        "status": status or "pending",
    }


def format_approval_notice(approval: dict[str, str]) -> str:
    return "\n".join(
        [
            "",
            "APPROVAZIONE RICHIESTA",
            f"Azione: {approval.get('action') or '(non specificata)'}",
            f"Request ID: {approval.get('request_id')}",
            f"Digest: {approval.get('digest') or '(non disponibile)'}",
            f"Stato: {approval.get('status') or 'pending'}",
            "",
            "Rispondi sul relativo messaggio Telegram:",
            "  approvo",
            "oppure:",
            "  rifiuto MOTIVO",
        ]
    )


def build_chat_payload(
    message: str,
    session: ChatSession,
    repo_context: dict[str, Any] | None,
    *,
    no_history: bool = False,
    stream: bool = True,
    provider: str | None = None,
) -> dict[str, Any]:
    payload = {
        "message": message,
        "history": [] if no_history else session.messages(),
        "cwd": session.cwd,
        "session_id": session.session_id,
        "model": session.model,
        "repo_context": repo_context if session.context_enabled else None,
        "stream": stream,
    }
    if provider:
        payload["provider"] = provider
    return payload


def build_task_payload(
    message: str,
    session: ChatSession,
    *,
    no_history: bool = False,
    provider: str | None = None,
    provider_endpoint: str | None = None,
    model_id: str | None = None,
    interaction_mode: str = "agent",
    capability: str | None = None,
) -> dict[str, Any]:
    terminal: dict[str, Any] = {
        "source": "ralf_terminal",
        "approval_gate": "telegram_required_for_protected_actions",
        "auto_execute_protected_actions": False,
        "cwd": session.cwd,
        "interaction_mode": interaction_mode,
    }
    if provider:
        terminal["provider"] = provider
    if provider_endpoint:
        terminal["provider_endpoint"] = provider_endpoint
    if model_id:
        terminal["model_id"] = model_id
    if capability:
        terminal["capability"] = capability
    if not no_history and session.turns:
        terminal["conversation_history"] = session.compact()
    return {
        "user_goal": message,
        "extra_context": {"source": "ralf_terminal", "terminal_client": terminal},
    }


def _provider_identity(config: ChatConfig, session: ChatSession) -> tuple[str | None, str | None]:
    if config.provider == "llama_cpp":
        llama = LlamaCppServerConfig.from_env()
        return llama.base_url, session.model or config.model or llama.model
    if config.provider == "ollama":
        return os.getenv("RALF_OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/"), session.model or config.model
    return None, session.model or config.model


def emit_response(
    payload: dict[str, Any],
    *,
    raw: bool = False,
    json_output: bool = False,
    out: TextIO = sys.stdout,
) -> int:
    safe_payload = sanitize_json(payload)
    approval = find_approval_request(payload)
    if json_output or raw:
        if json_output and approval:
            safe_payload = {**safe_payload, "terminal_approval": approval}
        print(json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True), file=out)
    else:
        print(sanitize_terminal_text(extract_text_response(payload)), file=out)
    if approval:
        print(sanitize_terminal_text(format_approval_notice(approval)), file=out)
    return 0


def _new_client(config: ChatConfig) -> RalfHTTPClient:
    return RalfHTTPClient(
        config.base_url,
        connect_timeout=config.connect_timeout,
        inactivity_timeout=config.inactivity_timeout,
        agent_timeout=config.agent_timeout,
    )


def _session_for_config(config: ChatConfig, store: SessionStore) -> ChatSession:
    if config.session_id:
        record = store.load(config.session_id)
    elif config.continue_session:
        record = store.latest()
    else:
        cwd = str(resolve_cwd(config.cwd or os.getcwd()))
        record = store.create(cwd=cwd, model=config.model)
    session = ChatSession.from_record(record, history_limit=config.history_limit)
    session.cwd = str(resolve_cwd(config.cwd or session.cwd))
    if config.model:
        session.model = config.model
    return session


def _save_session(store: SessionStore, session: ChatSession) -> None:
    record = session.to_record()
    store.save(record)
    session.created_at = str(record.get("created_at") or session.created_at)
    session.updated_at = str(record.get("updated_at") or session.updated_at)


def _context_for_session(session: ChatSession) -> dict[str, Any] | None:
    if not session.context_enabled:
        return None
    return collect_repo_context(session.cwd)


def _stream_chat(
    client: RalfHTTPClient,
    payload: dict[str, Any],
    *,
    raw: bool,
    prefix: bool,
    out: TextIO,
    err: TextIO,
) -> tuple[int, str, dict[str, Any]]:
    answer: list[str] = []
    info: dict[str, Any] = {"endpoint": CHAT_STREAM_ENDPOINT}
    saw_done = False
    done_ok = False
    saw_error = False
    saw_approval = False
    prefix_written = False
    try:
        events = client.post_chat_stream(payload)
        for event in events:
            event_type = event.get("type")
            if event_type == "start":
                info.update({key: event.get(key) for key in ("provider", "model", "session_id") if event.get(key)})
                event = {**event, "endpoint": CHAT_STREAM_ENDPOINT}
            elif event_type == "token":
                text = event.get("text")
                if not isinstance(text, str):
                    raise RalfTerminalError("invalid_token_event")
                text = sanitize_terminal_text(text)
                answer.append(text)
                if not raw:
                    if prefix and not prefix_written:
                        out.write("Ralf> ")
                        prefix_written = True
                    out.write(text)
                    out.flush()
            elif event_type == "approval":
                approval = find_approval_request({"approval": event})
                if approval:
                    saw_approval = True
                    info["approval"] = True
                    print(sanitize_terminal_text(format_approval_notice(approval)), file=out)
            elif event_type == "error":
                saw_error = True
                if not raw:
                    print(sanitize_terminal_text(str(event.get("message") or "chat_stream_error")), file=err)
            elif event_type == "done":
                saw_done = True
                done_ok = bool(event.get("ok", True))
                info.update({key: event.get(key) for key in ("provider", "model", "session_id") if event.get(key)})
                if isinstance(event.get("metadata"), dict):
                    info["metadata"] = event["metadata"]
            else:
                raise RalfTerminalError("unknown_stream_event")
            if raw:
                print(json.dumps(sanitize_json(event), ensure_ascii=False, sort_keys=True), file=out, flush=True)
    finally:
        if "events" in locals():
            close = getattr(events, "close", None)
            if callable(close):
                close()
    if prefix_written:
        print("", file=out)
    if not saw_done:
        raise RalfTerminalError("stream_missing_done")
    text = "".join(answer)
    if saw_error or not done_ok:
        return 1, text, info
    if not text:
        if saw_approval:
            return 0, "", info
        raise RalfTerminalError("empty_response")
    return 0, text, info


def _non_stream_chat(
    client: RalfHTTPClient,
    payload: dict[str, Any],
    *,
    raw: bool,
    json_output: bool,
    prefix: bool,
    out: TextIO,
) -> tuple[int, str, dict[str, Any]]:
    payload = {**payload, "stream": False}
    response = client.post_chat(payload)
    answer = sanitize_terminal_text(extract_text_response(response))
    if raw or json_output:
        emit_response(response, raw=raw, json_output=json_output, out=out)
    else:
        print(f"{'Ralf> ' if prefix else ''}{answer}", file=out)
    return 0, answer, {
        "endpoint": CHAT_ENDPOINT,
        "provider": response.get("provider"),
        "model": response.get("model"),
        "session_id": response.get("session_id"),
        "metadata": response.get("metadata") if isinstance(response.get("metadata"), dict) else {},
    }


def _perform_chat_turn(
    message: str,
    session: ChatSession,
    repo_context: dict[str, Any] | None,
    config: ChatConfig,
    client: RalfHTTPClient,
    store: SessionStore,
    *,
    prefix: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    payload = build_chat_payload(
        message,
        session,
        repo_context,
        no_history=config.no_history,
        stream=config.stream,
        provider=config.provider,
    )
    try:
        if config.stream and not config.json_output:
            try:
                rc, answer, info = _stream_chat(client, payload, raw=config.raw, prefix=prefix, out=out, err=err)
            except StreamFastPathUnavailable:
                print("stream fast-path non disponibile; uso /chat", file=err)
                rc, answer, info = _non_stream_chat(
                    client,
                    payload,
                    raw=config.raw,
                    json_output=config.json_output,
                    prefix=prefix,
                    out=out,
                )
        else:
            rc, answer, info = _non_stream_chat(
                client,
                payload,
                raw=config.raw,
                json_output=config.json_output,
                prefix=prefix,
                out=out,
            )
    except RalfTerminalError as exc:
        print(sanitize_terminal_text(str(exc)), file=err)
        return 1
    if rc != 0:
        return rc
    config.last_timings = info
    if config.timings:
        print(_timings_text(info), file=err)
    session.model = sanitize_terminal_text(str(info["model"])) if info.get("model") else session.model
    session.provider = sanitize_terminal_text(str(info["provider"])) if info.get("provider") else session.provider
    if answer:
        session.add(message, answer)
    _save_session(store, session)
    return 0


def run_ask(
    args: argparse.Namespace,
    *,
    client: RalfHTTPClient | None = None,
    store: SessionStore | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    config = config_from_args(args)
    if config.raw or config.json_output:
        config.stream = False
    store = store or SessionStore()
    text = sanitize_terminal_text(" ".join(args.message).strip())
    if not text:
        print("missing_message", file=err)
        return 2
    if config.provider in EXPERIMENTAL_PROVIDERS:
        print(f"EXPERIMENTAL PROVIDER: {config.provider}", file=err)
    if config.provider == "llama_cpp":
        print(_llama_cpp_details(), file=err)
    try:
        session = _session_for_config(config, store)
        repo_context = _context_for_session(session)
    except (RepoContextError, SessionStoreError, OSError) as exc:
        print(sanitize_terminal_text(str(exc)), file=err)
        return 2
    return _dispatch_natural_turn(
        text,
        session,
        repo_context,
        config,
        client or _new_client(config),
        store,
        prefix=False,
        out=out,
        err=err,
    )


AGENT_WARNING = """Modalita agente completa:
- puo impiegare diversi minuti;
- puo produrre richieste di approvazione;
- non eseguira azioni protette senza gate."""


def _run_agent_goal(
    goal: str,
    *,
    yes: bool,
    session: ChatSession,
    config: ChatConfig,
    client: RalfHTTPClient,
    input_func: Callable[[str], str],
    out: TextIO,
    err: TextIO,
    store: SessionStore | None = None,
    show_warning: bool = True,
    decision: InteractionDecision | None = None,
) -> int:
    selected_decision = decision or explicit_agent_decision(goal)
    if show_warning:
        print(AGENT_WARNING, file=out)
    if not yes:
        if input_func is input and not sys.stdin.isatty():
            print("agent non interattivo: usa --yes", file=err)
            return 2
        try:
            answer = input_func("Procedere? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("annullato", file=err)
            return 2
        if answer not in {"y", "yes", "s", "si"}:
            print("annullato", file=out)
            return 2
    endpoint, model_id = _provider_identity(config, session)
    print(
        f"interaction_mode=agent capability={selected_decision.capability}",
        file=err,
    )
    try:
        payload = client.post_task(
            build_task_payload(
                goal,
                session,
                no_history=config.no_history,
                provider=config.provider,
                provider_endpoint=endpoint,
                model_id=model_id,
                interaction_mode="agent",
                capability=selected_decision.capability,
            )
        )
    except KeyboardInterrupt:
        print("\ngenerazione agente interrotta", file=out)
        return 130
    except RalfTerminalError as exc:
        print(sanitize_terminal_text(str(exc)), file=err)
        return 1
    payload.setdefault("interaction_mode", "agent")
    payload.setdefault("capability", selected_decision.capability)
    envelope = payload.get("result_envelope")
    if isinstance(envelope, dict):
        meta = envelope.setdefault("meta", {})
        if isinstance(meta, dict):
            meta.setdefault("interaction_mode", "agent")
            meta.setdefault("capability", selected_decision.capability)
            meta.setdefault("provider", config.provider)
            meta.setdefault("endpoint", endpoint)
            meta.setdefault("model_id", model_id)
    rc = emit_response(payload, raw=config.raw, json_output=config.json_output, out=out)
    if rc == 0:
        answer = sanitize_terminal_text(extract_text_response(payload))
        if answer:
            session.add(goal, answer)
            if store is not None:
                _save_session(store, session)
    return rc


def _dispatch_natural_turn(
    message: str,
    session: ChatSession,
    repo_context: dict[str, Any] | None,
    config: ChatConfig,
    client: RalfHTTPClient,
    store: SessionStore,
    *,
    prefix: bool,
    out: TextIO,
    err: TextIO,
) -> int:
    decision = classify_interaction(message)
    if decision.interaction_mode == "unsupported":
        print(
            json.dumps(
                {
                    "interaction_mode": "unsupported",
                    "capability": decision.capability,
                    "tools_executed": False,
                    "command_count": 0,
                    "result_ids": [],
                    "execution_evidence": [],
                    "error": decision.reason,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=out,
        )
        return 2
    if decision.interaction_mode == "agent":
        return _run_agent_goal(
            message,
            yes=True,
            session=session,
            config=config,
            client=client,
            input_func=input,
            out=out,
            err=err,
            store=store,
            show_warning=False,
            decision=decision,
        )
    print(
        "interaction_mode=chat capability=chat_only tools_executed=false command_count=0",
        file=err,
    )
    return _perform_chat_turn(
        message,
        session,
        repo_context,
        config,
        client,
        store,
        prefix=prefix,
        out=out,
        err=err,
    )


def _run_agent_repl(
    config: ChatConfig,
    *,
    client: RalfHTTPClient,
    store: SessionStore,
    input_func: Callable[[str], str],
    out: TextIO,
    err: TextIO,
) -> int:
    try:
        session = _session_for_config(config, store)
    except (SessionStoreError, OSError) as exc:
        print(sanitize_terminal_text(str(exc)), file=err)
        return 2
    session.provider = config.provider
    _enable_readline()
    print(_styled("Ralf Agent", "1;36", out), file=out)
    print(sanitize_terminal_text(f"cwd: {session.cwd}"), file=out)
    print(sanitize_terminal_text(f"session: {session.session_id}"), file=out)
    print(sanitize_terminal_text(f"provider: {config.provider}"), file=out)
    print("mode: every message uses /tasks/run", file=out)
    print(AGENT_WARNING, file=out)
    while True:
        try:
            prompt = _styled("› ", "36", out) if sys.stdin.isatty() and _isatty(out) else ""
            message = input_func(prompt)
        except EOFError:
            if _isatty(out):
                print("", file=out)
            return 0
        except KeyboardInterrupt:
            print("\ninterrotto", file=out)
            continue
        message = sanitize_terminal_text(message.strip())
        if not message:
            continue
        command = message.partition(" ")[0]
        if command in {"/exit", "/quit"}:
            return 0
        if command == "/help":
            print("/status /history /reset /endpoint /session /exit", file=out)
            continue
        if command in {"/clear", "/reset"}:
            session.reset()
            _save_session(store, session)
            print("cronologia azzerata", file=out)
            continue
        if command == "/history":
            print(_history_text(session), file=out)
            continue
        if command == "/status":
            print(_status_text(client), file=out)
            continue
        if command == "/endpoint":
            print(_endpoint_text(config), file=out)
            continue
        if command == "/session":
            print(_session_text(session), file=out)
            continue
        if command.startswith("/"):
            print("comando sconosciuto; usa /help", file=err)
            continue
        try:
            _run_agent_goal(
                message,
                yes=True,
                session=session,
                config=config,
                client=client,
                input_func=input_func,
                out=out,
                err=err,
                store=store,
                show_warning=False,
                decision=explicit_agent_decision(message),
            )
        except (SessionStoreError, OSError) as exc:
            print(sanitize_terminal_text(str(exc)), file=err)


def run_agent(
    args: argparse.Namespace,
    *,
    client: RalfHTTPClient | None = None,
    store: SessionStore | None = None,
    input_func: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    config = config_from_args(args)
    requested_provider = getattr(args, "provider", None)
    if requested_provider and requested_provider not in {"llama_cpp", "ollama"}:
        print("experimental_provider_not_allowed_for_agent", file=err)
        return 2
    if config.provider not in {"llama_cpp", "ollama"}:
        print("experimental_provider_not_allowed_for_agent", file=err)
        return 2
    selected_client = client or _new_client(config)
    goal = sanitize_terminal_text(" ".join(getattr(args, "message", []) or []).strip())
    if not goal:
        return _run_agent_repl(
            config,
            client=selected_client,
            store=store or SessionStore(),
            input_func=input_func,
            out=out,
            err=err,
        )
    session = ChatSession(
        session_id="agent",
        cwd=str(resolve_cwd(config.cwd or os.getcwd())),
        provider=config.provider,
    )
    return _run_agent_goal(
        goal,
        yes=bool(getattr(args, "yes", False)),
        session=session,
        config=config,
        client=selected_client,
        input_func=input_func,
        out=out,
        err=err,
        decision=explicit_agent_decision(goal),
    )


def run_doctor_command(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    from ralfloop_agent.doctor import render_doctor, run_doctor

    try:
        report = run_doctor(cwd=getattr(args, "cwd", None) or os.getcwd())
    except (OSError, ValueError) as exc:
        print(sanitize_terminal_text(str(exc)), file=err)
        return 2
    if getattr(args, "json", False):
        print(report.model_dump_json(indent=2), file=out)
    else:
        print(render_doctor(report), file=out)
    return 0 if report.ok else 1


def _repair_backend_request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    import requests

    base_url = (
        os.getenv("RALF_REPAIR_BACKEND_URL")
        or os.getenv("RALFLOOP_BACKEND_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")

    try:
        response = requests.request(
            method,
            f"{base_url}{path}",
            json=payload,
            timeout=(DEFAULT_CONNECT_TIMEOUT, timeout),
        )
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise OSError(
            f"repair_backend_unavailable:{exc}"
        ) from exc
    except ValueError as exc:
        raise OSError(
            "repair_backend_invalid_json"
        ) from exc
    finally:
        try:
            response.close()
        except UnboundLocalError:
            pass

    if not isinstance(data, dict):
        raise OSError("repair_backend_invalid_response")

    return data


def run_repair_command(
    args: argparse.Namespace,
    *,
    manager=None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    action = args.repair_action

    try:
        # Dependency injection remains local only for tests/library callers.
        if manager is not None and action in {
            "plan",
            "run",
            "status",
        }:
            if action == "status":
                record = manager.status(args.run_id)
            else:
                description = sanitize_terminal_text(
                    " ".join(args.description).strip()
                )

                if not description:
                    print(
                        "missing_repair_description",
                        file=err,
                    )
                    return 2

                record = (
                    manager.plan(description)
                    if action == "plan"
                    else manager.run(description)
                )

            result = record.model_dump(mode="json")

        elif action == "request-approval":
            result = _repair_backend_request(
                "POST",
                f"/repairs/{args.run_id}/approval-request",
                payload={
                    "requested_by": "ralf_repair_cli",
                },
                timeout=30.0,
            )

        elif action == "apply":
            result = _repair_backend_request(
                "POST",
                f"/repair-approvals/{args.request_id}/apply",
                timeout=60.0,
            )

        elif action == "status":
            result = _repair_backend_request(
                "GET",
                f"/repairs/{args.run_id}",
                timeout=30.0,
            )

        else:
            description = sanitize_terminal_text(
                " ".join(args.description).strip()
            )

            if not description:
                print(
                    "missing_repair_description",
                    file=err,
                )
                return 2

            payload: dict[str, Any] = {
                "description": description,
            }

            repo = getattr(args, "repo", None)
            if repo:
                payload["repo"] = str(repo)

            result = _repair_backend_request(
                "POST",
                f"/repairs/{action}",
                payload=payload,
                timeout=(
                    DEFAULT_AGENT_TIMEOUT
                    if action == "run"
                    else 60.0
                ),
            )

    except (KeyError, OSError, ValueError) as exc:
        print(
            sanitize_terminal_text(str(exc)),
            file=err,
        )
        return 2

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        file=out,
    )

    if action == "request-approval":
        request_id = (
            result.get("request_id")
            or (
                (result.get("request") or {}).get("request_id")
                if isinstance(result.get("request"), dict)
                else None
            )
        )
        return 0 if request_id else 1

    if action == "apply":
        return 0 if result.get("status") in {
            "executed",
            "already_executed",
        } else 1

    return 0 if result.get("status") in {
        "planned",
        "approval_pending",
        "approval_requested",
        "applied",
    } else 1


def _parse_portal_operations(raw: str) -> list[dict[str, Any]]:
    try:
        operations = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("portal_operations_json_invalid") from exc

    if not isinstance(operations, list):
        raise ValueError("portal_operations_must_be_array")
    if not 1 <= len(operations) <= 64:
        raise ValueError("portal_operations_count_invalid")
    if any(not isinstance(operation, dict) for operation in operations):
        raise ValueError("portal_operation_must_be_object")
    return operations


def _portal_result_succeeded(result: dict[str, Any]) -> bool:
    if result.get("ok") is False:
        return False
    status = result.get("status")
    if status is None:
        return True
    return str(status) in {
        "FOUND",
        "PARTIAL",
        "already_executed",
        "approved",
        "consumed",
        "executed",
        "ok",
        "pending",
        "preview",
    }


def run_portal_command(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    action = args.portal_action

    try:
        if action == "arci-profile":
            result = _repair_backend_request(
                "GET",
                "/portals/arci/profile",
            )
        elif action in {
            "arci-club", "arci-cards", "arci-committee", "arci-regional",
            "arci-dashboard-alerts",
        }:
            resource = action.removeprefix("arci-")
            result = _repair_backend_request("GET", f"/portals/arci/{resource}")
        elif action == "support4youth-snapshot":
            result = _repair_backend_request(
                "GET",
                "/portals/support4youth/snapshot",
            )
        elif action == "support4youth-send-updates-preview":
            result = _repair_backend_request(
                "GET",
                "/portals/support4youth/send-updates/preview",
            )
        elif action == "support4youth-send-updates-request":
            result = _repair_backend_request(
                "POST",
                "/portals/support4youth/send-updates/requests",
                payload={"requested_by": "ralf_portal_cli"},
            )
        elif action in {
            "support4youth-preview",
            "support4youth-request",
        }:
            operations = _parse_portal_operations(args.operations_json)
            payload: dict[str, Any] = {"operations": operations}
            if action == "support4youth-request":
                payload["requested_by"] = "ralf_portal_cli"
            result = _repair_backend_request(
                "POST",
                (
                    "/portals/support4youth/preview"
                    if action == "support4youth-preview"
                    else "/portals/support4youth/requests"
                ),
                payload=payload,
            )
        elif action == "support4youth-apply":
            request_id = str(args.request_id)
            if not re.fullmatch(r"apr_[A-Z2-9]{8}", request_id):
                raise ValueError("portal_request_id_invalid")
            result = _repair_backend_request(
                "POST",
                f"/portals/support4youth/requests/{request_id}/apply",
                timeout=60.0,
            )
        else:
            raise ValueError("portal_action_invalid")
    except (KeyError, OSError, ValueError) as exc:
        print(sanitize_terminal_text(str(exc)), file=err)
        return 2

    print(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        file=out,
    )
    return 0 if _portal_result_succeeded(result) else 1


def run_chat(
    args: argparse.Namespace,
    *,
    client: RalfHTTPClient | None = None,
    store: SessionStore | None = None,
    input_func: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    config = config_from_args(args)
    client = client or _new_client(config)
    store = store or SessionStore()
    try:
        session = _session_for_config(config, store)
        repo_context = _context_for_session(session)
    except (RepoContextError, SessionStoreError, OSError) as exc:
        print(sanitize_terminal_text(str(exc)), file=err)
        return 2
    _enable_readline()
    _print_banner(session, config, out)
    while True:
        try:
            prompt = _styled("› ", "36", out) if sys.stdin.isatty() and _isatty(out) else ""
            message = input_func(prompt)
        except EOFError:
            if _isatty(out):
                print("", file=out)
            return 0
        except KeyboardInterrupt:
            print("\ninterrotto", file=out)
            continue
        message = sanitize_terminal_text(message.strip())
        if not message:
            continue
        command, _, argument = message.partition(" ")
        if command in {"/exit", "/quit"}:
            return 0
        if command == "/help":
            print(_help_text(), file=out)
            continue
        if command in {"/clear", "/reset"}:
            session.reset()
            _save_session(store, session)
            print("cronologia azzerata", file=out)
            continue
        if command == "/raw":
            config.raw = not config.raw
            print(f"raw={'on' if config.raw else 'off'}", file=out)
            continue
        if command == "/stream":
            config.stream = not config.stream
            print(f"stream={'on' if config.stream else 'off'}", file=out)
            continue
        if command == "/history":
            print(_history_text(session), file=out)
            continue
        if command == "/status":
            print(_status_text(client), file=out)
            continue
        if command == "/endpoint":
            print(_endpoint_text(config), file=out)
            continue
        if command == "/tools":
            print("chat: nessuno; agent: workflow esplicito con gate", file=out)
            continue
        if command == "/session":
            print(_session_text(session), file=out)
            continue
        if command == "/new":
            record = store.create(cwd=session.cwd, model=session.model, context_enabled=session.context_enabled)
            session = ChatSession.from_record(record, history_limit=config.history_limit)
            repo_context = _context_for_session(session)
            print(f"session={session.session_id}", file=out)
            continue
        if command == "/model":
            if argument.strip():
                session.model = None if argument.strip() == "default" else argument.strip()
                _save_session(store, session)
            print(f"model={session.model or 'configured'} provider={session.provider or 'configured'}", file=out)
            continue
        if command == "/provider":
            requested = argument.strip().lower()
            if requested:
                if requested not in CHAT_PROVIDERS:
                    print(f"uso: /provider {'|'.join(CHAT_PROVIDERS)}", file=err)
                    continue
                config.provider = requested
            print(f"provider={config.provider}", file=out)
            if config.provider in EXPERIMENTAL_PROVIDERS:
                print(f"EXPERIMENTAL PROVIDER: {config.provider}", file=out)
            if config.provider == "llama_cpp":
                print(_llama_cpp_endpoint_details(), file=out)
            continue
        if command == "/engine":
            action = argument.strip().lower() or "status"
            action = action.replace("-", "_").replace(" ", "_")
            if action not in {"status", "start", "stop", "health", "switch_chat", "switch_agent", "handoff_status"}:
                print("uso: /engine [status|start|stop|health|switch chat|switch agent|handoff-status]", file=err)
                continue
            run_engine_action(action, out=out, err=err)
            continue
        if command == "/timings":
            print(_timings_text(config.last_timings) if config.last_timings else "(nessun timing)", file=out)
            continue
        if command == "/cwd":
            if argument.strip():
                old_cwd = session.cwd
                old_context = repo_context
                try:
                    requested = Path(argument.strip()).expanduser()
                    if not requested.is_absolute():
                        requested = Path(session.cwd) / requested
                    candidate_cwd = str(resolve_cwd(requested))
                    candidate_context = collect_repo_context(candidate_cwd) if session.context_enabled else None
                    session.cwd = candidate_cwd
                    repo_context = candidate_context
                    _save_session(store, session)
                except (RepoContextError, SessionStoreError, OSError) as exc:
                    session.cwd = old_cwd
                    repo_context = old_context
                    print(sanitize_terminal_text(str(exc)), file=err)
                    continue
            print(f"cwd={session.cwd}", file=out)
            continue
        if command == "/context":
            action = argument.strip().lower() or "show"
            if action == "on":
                session.context_enabled = True
                repo_context = _context_for_session(session)
                _save_session(store, session)
                print("context=on", file=out)
            elif action == "off":
                session.context_enabled = False
                repo_context = None
                _save_session(store, session)
                print("context=off", file=out)
            elif action == "refresh":
                try:
                    repo_context = _context_for_session(session)
                    print("context=refreshed", file=out)
                except RepoContextError as exc:
                    print(sanitize_terminal_text(str(exc)), file=err)
            elif action == "show":
                print(json.dumps(sanitize_json(repo_context), ensure_ascii=False, indent=2, sort_keys=True), file=out)
            else:
                print("uso: /context [show|refresh|on|off]", file=err)
            continue
        if command == "/agent":
            goal = argument.strip()
            if not goal:
                print("missing_goal", file=err)
                continue
            _run_agent_goal(
                goal,
                yes=False,
                session=session,
                config=config,
                client=client,
                input_func=input_func,
                out=out,
                err=err,
                store=store,
                decision=explicit_agent_decision(goal),
            )
            continue
        if command.startswith("/"):
            print("comando sconosciuto; usa /help", file=err)
            continue
        try:
            _dispatch_natural_turn(
                message,
                session,
                repo_context,
                config,
                client,
                store,
                prefix=True,
                out=out,
                err=err,
            )
        except KeyboardInterrupt:
            print("\ngenerazione interrotta", file=out)
            continue
        except (SessionStoreError, OSError) as exc:
            print(sanitize_terminal_text(str(exc)), file=err)


def _llama_cpp_details(manager: LlamaCppServerManager | None = None) -> str:
    selected = manager or LlamaCppServerManager()
    return sanitize_terminal_text(
        f"provider: llama_cpp\nmodel: {selected.config.model}\nendpoint: {selected.config.base_url}"
    )


def _llama_cpp_endpoint_details(manager: LlamaCppServerManager | None = None) -> str:
    selected = manager or LlamaCppServerManager()
    return sanitize_terminal_text(
        f"model: {selected.config.model}\nendpoint: {selected.config.base_url}"
    )


def _timings_text(info: dict[str, Any]) -> str:
    metadata = info.get("metadata") if isinstance(info.get("metadata"), dict) else {}
    fields = {
        "provider": info.get("provider") or metadata.get("provider"),
        "provider_requested": metadata.get("provider_requested"),
        "provider_effective": metadata.get("provider_effective"),
        "model": info.get("model") or metadata.get("model"),
        "endpoint": metadata.get("endpoint") or info.get("endpoint"),
        "server_startup_ms": metadata.get("server_startup_ms"),
        "server_reused": metadata.get("server_reused"),
        "ttft_ms": metadata.get("ttft_ms"),
        "wall_ms": metadata.get("wall_ms"),
        "prompt_count": metadata.get("prompt_tokens"),
        "generated_count": metadata.get("generated_tokens"),
        "prompt_eval_ms": metadata.get("prompt_eval_ms"),
        "prompt_rate": metadata.get("prompt_tokens_per_second"),
        "decode_rate": metadata.get("decode_tokens_per_second"),
        "cache_hit_count": metadata.get("cache_hit_tokens"),
        "gpu_layers": metadata.get("gpu_layers"),
        "fallback_used": metadata.get("fallback_used"),
        "fallback_reason": metadata.get("fallback_reason"),
    }
    return json.dumps(
        sanitize_json({key: value for key, value in fields.items() if value is not None}),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def run_engine_action(
    action: str,
    *,
    manager: LlamaCppServerManager | None = None,
    coordinator: AgentGpuCoordinator | None = None,
    dry_run: bool = False,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    selected = manager or LlamaCppServerManager()
    try:
        if action == "start":
            result = selected.start(dry_run=dry_run, detach=not dry_run)
        elif action == "stop":
            result = selected.stop()
        elif action == "health":
            result = selected.status()
        elif action == "status":
            result = selected.status()
        elif action == "switch_chat":
            result = (coordinator or AgentGpuCoordinator(server_manager=selected)).switch_chat()
        elif action == "switch_agent":
            result = (coordinator or AgentGpuCoordinator(server_manager=selected)).switch_agent()
        elif action == "handoff_status":
            result = (coordinator or AgentGpuCoordinator(server_manager=selected)).handoff_status()
        else:
            print("unknown_engine_action", file=err)
            return 2
    except (LlamaCppServerError, AgentGpuHandoffError, OSError, ValueError) as exc:
        code = exc.code if isinstance(exc, (LlamaCppServerError, AgentGpuHandoffError)) else "llama_cpp_engine_configuration_error"
        print(sanitize_terminal_text(code), file=err)
        return 1
    print(json.dumps(sanitize_json(result), ensure_ascii=False, indent=2, sort_keys=True), file=out)
    return 0 if action != "health" or result.get("healthy") is True else 1


def run_engine(
    args: argparse.Namespace,
    *,
    manager: LlamaCppServerManager | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    action = args.engine_action.replace("-", "_")
    if action == "switch":
        action = f"switch_{args.mode}"
    if getattr(args, "engine", "llama_cpp") != "llama_cpp":
        print("unsupported_engine", file=err)
        return 2
    return run_engine_action(
        action,
        manager=manager,
        dry_run=bool(getattr(args, "dry_run", False)),
        out=out,
        err=err,
    )


def run_domain_reason(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    from ralfloop_agent.domains.reasoning_cli import reason_domain

    result = reason_domain(args.domain_id, " ".join(args.question), recursive=bool(args.recursive))
    print(json.dumps(sanitize_json(result), ensure_ascii=False, indent=2, sort_keys=True), file=out)
    status = str(result.get("status") or "")
    return 3 if status == "domain_creation_required" else 4 if result.get("jury_status") == "disabled" else 0


def run_recursive(
    args: argparse.Namespace,
    *,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    from ralfloop_agent.domains.reasoning_cli import explain_routing, latest_benchmark_summary

    if args.recursive_action == "explain-routing":
        result = explain_routing(args.domain_id, " ".join(args.question))
    elif args.recursive_action == "benchmark-domain":
        result = latest_benchmark_summary()
    else:
        print("unknown_recursive_action", file=err)
        return 2
    print(json.dumps(sanitize_json(result), ensure_ascii=False, indent=2, sort_keys=True), file=out)
    return 0 if result.get("selected_backend") != "domain_creation_required" else 3


def run_sessions(
    args: argparse.Namespace,
    *,
    store: SessionStore | None = None,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    store = store or SessionStore()
    try:
        if args.sessions_command == "show":
            print(json.dumps(sanitize_json(store.load(args.session_id)), ensure_ascii=False, indent=2, sort_keys=True), file=out)
            return 0
        if args.sessions_command == "delete":
            store.delete(args.session_id)
            print(f"deleted={args.session_id}", file=out)
            return 0
        rows = store.list()
    except SessionStoreError as exc:
        print(sanitize_terminal_text(str(exc)), file=err)
        return 1
    if not rows:
        print("(nessuna sessione)", file=out)
        return 0
    for row in rows:
        print(
            sanitize_terminal_text(
                f"{row['session_id']}  {row['updated_at']}  turns={row['turns']}  "
                f"model={row['model'] or 'configured'}  cwd={row['cwd']}"
            ),
            file=out,
        )
    return 0


def _help_text() -> str:
    return "\n".join(
        [
            "/help",
            "/status",
            "/model [MODEL|default]",
            "/provider [ollama|llama_cpp|remote_tool|speculative_local|speculative_remote]",
            "/engine [status|start|stop|health|switch chat|switch agent|handoff-status]",
            "/timings",
            "/cwd [PERCORSO]",
            "/session",
            "/new",
            "/history",
            "/clear",
            "/raw",
            "/stream",
            "/tools",
            "/context [show|refresh|on|off]",
            "/agent OBIETTIVO",
            "/exit",
            "/quit",
        ]
    )


def _history_text(session: ChatSession) -> str:
    if not session.turns:
        return "(cronologia vuota)"
    rows: list[str] = []
    for idx, turn in enumerate(session.turns, start=1):
        rows.append(f"{idx}. Tu: {sanitize_terminal_text(turn.user[:120])}")
        rows.append(f"   Ralf: {sanitize_terminal_text(turn.ralf[:120])}")
    return "\n".join(rows)


def _endpoint_text(config: ChatConfig) -> str:
    return json.dumps(
        {
            "chat_stream": f"{config.base_url}{CHAT_STREAM_ENDPOINT}",
            "chat_fallback": f"{config.base_url}{CHAT_ENDPOINT}",
            "agent_explicit": f"{config.base_url}{TASK_ENDPOINT}",
            "normal_chat_uses_tasks_run": True,
            "routing": "deterministic_natural_dispatch",
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _status_text(client: RalfHTTPClient) -> str:
    status: dict[str, Any] = {
        "backend": client.base_url,
        "chat": f"{client.base_url}{CHAT_ENDPOINT}",
        "chat_stream": f"{client.base_url}{CHAT_STREAM_ENDPOINT}",
        "agent": f"{client.base_url}{TASK_ENDPOINT}",
    }
    try:
        paths = client.get_json("/openapi.json").get("paths", {})
        status["chat_available"] = CHAT_ENDPOINT in paths
        status["chat_stream_available"] = CHAT_STREAM_ENDPOINT in paths
        status["agent_available"] = TASK_ENDPOINT in paths
    except RalfTerminalError as exc:
        status["openapi"] = str(exc)
    try:
        status["domain_approval_health"] = sanitize_json(client.get_json("/domain-approvals/health"))
    except RalfTerminalError as exc:
        status["domain_approval_health"] = str(exc)
    return json.dumps(sanitize_json(status), ensure_ascii=False, indent=2, sort_keys=True)


def _session_text(session: ChatSession) -> str:
    return json.dumps(
        sanitize_json(
            {
            "session_id": session.session_id,
            "cwd": session.cwd,
            "model": session.model,
            "provider": session.provider,
            "turns": len(session.turns),
            "context_enabled": session.context_enabled,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            }
        ),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _print_banner(session: ChatSession, config: ChatConfig, out: TextIO) -> None:
    print(_styled("Ralf", "1;36", out), file=out)
    print(sanitize_terminal_text(f"cwd: {session.cwd}"), file=out)
    print(sanitize_terminal_text(f"model: {session.model or 'configured'}"), file=out)
    print(sanitize_terminal_text(f"session: {session.session_id}"), file=out)
    print(sanitize_terminal_text(f"provider: {config.provider}"), file=out)
    if config.provider in EXPERIMENTAL_PROVIDERS:
        print(sanitize_terminal_text(f"EXPERIMENTAL PROVIDER: {config.provider}"), file=out)
    if config.provider == "llama_cpp":
        print(_llama_cpp_endpoint_details(), file=out)


def _isatty(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _styled(text: str, code: str, out: TextIO) -> str:
    if os.getenv("NO_COLOR") is not None or not _isatty(out):
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def _enable_readline() -> None:
    try:
        import readline  # noqa: F401
    except ImportError:
        pass


def config_from_args(args: argparse.Namespace) -> ChatConfig:
    config = ChatConfig.from_env()
    if getattr(args, "base_url", None):
        config.base_url = args.base_url.rstrip("/")
    if getattr(args, "timeout", None) is not None:
        config.agent_timeout = float(args.timeout)
    if getattr(args, "connect_timeout", None) is not None:
        config.connect_timeout = float(args.connect_timeout)
    if getattr(args, "inactivity_timeout", None) is not None:
        config.inactivity_timeout = float(args.inactivity_timeout)
    if hasattr(args, "stream"):
        config.stream = bool(args.stream)
    config.raw = bool(getattr(args, "raw", False))
    config.json_output = bool(getattr(args, "json", False))
    config.no_history = bool(getattr(args, "no_history", False))
    config.timings = bool(getattr(args, "timings", config.timings))
    config.continue_session = bool(getattr(args, "continue_session", False))
    config.session_id = getattr(args, "session", None)
    config.cwd = getattr(args, "cwd", None)
    config.model = getattr(args, "model", None)
    if getattr(args, "provider", None):
        config.provider = args.provider
    return config


def _add_common_options(parser: argparse.ArgumentParser, *, suppress_defaults: bool) -> None:
    default = argparse.SUPPRESS if suppress_defaults else None
    false_default = argparse.SUPPRESS if suppress_defaults else False
    parser.add_argument("--base-url", default=default, help=f"backend URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--timeout", type=float, default=default, help="agent request timeout seconds")
    parser.add_argument("--connect-timeout", type=float, default=default, help="backend connect timeout seconds")
    parser.add_argument("--inactivity-timeout", type=float, default=default, help="stream inactivity timeout seconds")
    parser.add_argument("--raw", action="store_true", default=false_default, help="print redacted protocol events")
    parser.add_argument("--no-history", action="store_true", default=false_default, help="omit conversation history")
    parser.add_argument("--continue", dest="continue_session", action="store_true", default=false_default, help="resume latest session")
    parser.add_argument("--session", default=default, help="resume session ID")
    parser.add_argument("--cwd", default=default, help="repository working directory")
    parser.add_argument("--model", default=default, help="local model override")
    parser.add_argument("--provider", choices=CHAT_PROVIDERS, default=default, help="chat provider; experimental providers are opt-in")
    parser.add_argument("--timings", action="store_true", default=false_default, help="print provider timing metadata")
    stream = parser.add_mutually_exclusive_group()
    stream.add_argument("--stream", dest="stream", action="store_true", default=argparse.SUPPRESS)
    stream.add_argument("--no-stream", dest="stream", action="store_false", default=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ralf", description="Ralf terminal client")
    _add_common_options(parser, suppress_defaults=False)
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="start persistent fast-path chat")
    _add_common_options(chat, suppress_defaults=True)

    ask = sub.add_parser("ask", help="send one fast-path chat message")
    _add_common_options(ask, suppress_defaults=True)
    ask.add_argument("--json", action="store_true", help="print complete JSON response")
    ask.add_argument("message", nargs="+", help="message for Ralf")

    agent = sub.add_parser("agent", help="start agentic chat or run one explicit workflow")
    _add_common_options(agent, suppress_defaults=True)
    agent.add_argument("--yes", action="store_true", help="confirm full workflow in non-interactive mode")
    agent.add_argument("message", nargs="*", help="optional one-shot agent objective")

    email = sub.add_parser("email", help="deterministic email operations")
    email_sub = email.add_subparsers(dest="email_action", required=True)
    reconcile = email_sub.add_parser(
        "reconcile", help="read-only provider reconciliation; never sends email"
    )
    reconcile.add_argument("--approval", required=True)
    reconcile.add_argument("--otp-request", required=True)
    reconcile.add_argument("--thread", required=True)
    renew = email_sub.add_parser(
        "renew-approval", help="fresh approval after deterministic NOT_SENT proof"
    )
    renew.add_argument("--from-approval", required=True)
    renew.add_argument("--otp-request", required=True)
    renew.add_argument("--thread", required=True)
    renew.add_argument("--session", required=True)

    doctor = sub.add_parser("doctor", help="run read-only Ralf diagnostics")
    doctor.add_argument("--json", action="store_true", help="print structured JSON")
    doctor.add_argument("--cwd", help="directory metadata to inspect")

    repair = sub.add_parser("repair", help="plan or run bounded repair in an isolated worktree")
    repair_sub = repair.add_subparsers(dest="repair_action", required=True)
    for action in ("plan", "run"):
        command = repair_sub.add_parser(action)
        command.add_argument(
            "--repo",
            help="source Git repository; defaults to configured canonical repository",
        )
        command.add_argument("description", nargs="+", help="bounded repair description")
    repair_status = repair_sub.add_parser("status")
    repair_status.add_argument("run_id")
    repair_request = repair_sub.add_parser(
        "request-approval",
        help="bind a verified repair to persistent Ralf approval",
    )
    repair_request.add_argument("run_id")
    repair_apply = repair_sub.add_parser(
        "apply",
        help="apply one approved hash-bound repair exactly once",
    )
    repair_apply.add_argument("request_id")

    portal = sub.add_parser("portal", help="approval-bound organization portal operations")
    portal_sub = portal.add_subparsers(dest="portal_action", required=True)
    portal_sub.add_parser("arci-profile", help="read sanitized ARCI organization profile")
    portal_sub.add_parser("arci-club", help="read sanitized current ARCI club")
    portal_sub.add_parser("arci-cards", help="read sanitized current ARCI card states")
    portal_sub.add_parser("arci-committee", help="read current ARCI committee")
    portal_sub.add_parser("arci-regional", help="read current ARCI regional organization")
    portal_sub.add_parser("arci-dashboard-alerts", help="read sanitized ARCI dashboard alerts")
    portal_sub.add_parser(
        "support4youth-snapshot",
        help="read sanitized Support4Youth profile snapshot",
    )
    portal_sub.add_parser(
        "support4youth-send-updates-preview",
        help="preview final Send updates operation",
    )
    portal_sub.add_parser(
        "support4youth-send-updates-request",
        help="request separate approval for final Send updates operation",
    )
    for action in ("support4youth-preview", "support4youth-request"):
        command = portal_sub.add_parser(action)
        command.add_argument(
            "--operations-json",
            required=True,
            help="JSON array containing 1..64 portal operations",
        )
    portal_apply = portal_sub.add_parser("support4youth-apply")
    portal_apply.add_argument("request_id")

    engine = sub.add_parser("engine", help="manage the user-space inference engine")
    engine_sub = engine.add_subparsers(dest="engine_action", required=True)
    for action in ("status", "start", "stop", "health"):
        action_parser = engine_sub.add_parser(action)
        action_parser.add_argument("engine", nargs="?", default="llama_cpp", choices=("llama_cpp",))
        if action == "start":
            action_parser.add_argument("--dry-run", action="store_true")
    switch = engine_sub.add_parser("switch", help="switch the shared GPU to chat or agent mode")
    switch.add_argument("mode", choices=("chat", "agent"))
    engine_sub.add_parser("handoff-status", help="show shared GPU handoff state")

    domain = sub.add_parser("domain", help="domain-aware operations")
    domain_sub = domain.add_subparsers(dest="domain_action", required=True)
    reason = domain_sub.add_parser("reason", help="reason inside one resolved domain")
    reason.add_argument("--recursive", action="store_true", help="explicit RecursiveMAS request")
    reason.add_argument("domain_id")
    reason.add_argument("question", nargs="+")

    recursive = sub.add_parser("recursive", help="RecursiveMAS routing and benchmark diagnostics")
    recursive_sub = recursive.add_subparsers(dest="recursive_action", required=True)
    explain = recursive_sub.add_parser("explain-routing", help="show routing decision without chain of thought")
    explain.add_argument("domain_id")
    explain.add_argument("question", nargs="+")
    recursive_sub.add_parser("benchmark-domain", help="show latest domain-reasoning benchmark")

    sessions = sub.add_parser("sessions", help="list persistent sessions")
    session_sub = sessions.add_subparsers(dest="sessions_command")
    show = session_sub.add_parser("show", help="show one session")
    show.add_argument("session_id")
    delete = session_sub.add_parser("delete", help="delete one session")
    delete.add_argument("session_id")

    from ralfloop_agent.glm_review.cli import add_approval_parsers, add_glm_parser
    from ralfloop_agent.local_arch.cli import add_local_arch_parsers

    add_glm_parser(sub)
    add_approval_parsers(sub)
    add_local_arch_parsers(sub)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_standard_streams_utf8()
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        if args.command in {None, "chat"}:
            return run_chat(args)
        if args.command == "ask":
            return run_ask(args)
        if args.command == "agent":
            return run_agent(args)
        if args.command == "email" and args.email_action == "reconcile":
            from ralfloop_agent.unified_assistant.email_reconcile import EmailReconciler

            result = EmailReconciler.from_environment().reconcile(
                approval_id=args.approval,
                otp_request_id=args.otp_request,
                thread_id=args.thread,
            )
            print(result.lines())
            return 0
        if args.command == "email" and args.email_action == "renew-approval":
            from ralfloop_agent.unified_assistant.email_renewal import EmailApprovalRenewal

            result = EmailApprovalRenewal.from_environment().renew(
                previous_approval_id=args.from_approval,
                previous_otp_request_id=args.otp_request,
                thread_id=args.thread,
                session_id=args.session,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0 if result.get("status") == "pending" else 2
        if args.command == "doctor":
            return run_doctor_command(args)
        if args.command == "repair":
            return run_repair_command(args)
        if args.command == "portal":
            return run_portal_command(args)
        if args.command == "engine":
            return run_engine(args)
        if args.command == "domain" and args.domain_action == "reason":
            return run_domain_reason(args)
        if args.command == "recursive":
            return run_recursive(args)
        if args.command == "sessions":
            return run_sessions(args)
        if args.command in {"glm", "approve", "reject"}:
            from ralfloop_agent.glm_review.cli import run as run_glm

            return run_glm(args)
        if args.command in {"router", "evolve", "visual", "audiobook", "media"}:
            from ralfloop_agent.local_arch.cli import run_local_arch

            return run_local_arch(args)
    except (RalfTerminalError, RepoContextError, SessionStoreError) as exc:
        print(sanitize_terminal_text(str(exc)), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrotto", file=sys.stderr)
        return 130
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
