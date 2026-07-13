from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, TextIO

DEFAULT_BASE_URL = "http://127.0.0.1:19090"
DEFAULT_TIMEOUT = 120.0
DEFAULT_HISTORY_LIMIT = 12
TASK_ENDPOINT = "/tasks/run"

TEXT_KEYS = ("answer", "final_answer", "response", "message", "text", "output", "result", "data", "payload")
SECRET_MARKERS = ("secret", "token", "hmac", "key", "authorization", "cookie", "password", "nonce")


class RalfTerminalError(Exception):
    """User-facing terminal client error."""


class BackendUnavailable(RalfTerminalError):
    pass


class BackendTimeout(RalfTerminalError):
    pass


class BackendHTTPError(RalfTerminalError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


@dataclass
class ChatConfig:
    base_url: str = DEFAULT_BASE_URL
    timeout: float = DEFAULT_TIMEOUT
    history_limit: int = DEFAULT_HISTORY_LIMIT
    raw: bool = False
    json_output: bool = False
    no_history: bool = False

    @classmethod
    def from_env(cls) -> "ChatConfig":
        return cls(
            base_url=os.getenv("RALF_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            timeout=_env_float("RALF_CHAT_TIMEOUT", DEFAULT_TIMEOUT),
            history_limit=max(1, _env_int("RALF_CHAT_HISTORY_LIMIT", DEFAULT_HISTORY_LIMIT)),
        )


@dataclass
class Turn:
    user: str
    ralf: str


@dataclass
class ChatSession:
    history_limit: int = DEFAULT_HISTORY_LIMIT
    turns: list[Turn] = field(default_factory=list)

    def add(self, user: str, ralf: str) -> None:
        self.turns.append(Turn(user=user, ralf=ralf))
        if len(self.turns) > self.history_limit:
            self.turns = self.turns[-self.history_limit :]

    def reset(self) -> None:
        self.turns.clear()

    def compact(self) -> list[dict[str, str]]:
        return [{"user": t.user, "ralf": t.ralf} for t in self.turns]


class RalfHTTPClient:
    def __init__(self, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def task_url(self) -> str:
        return f"{self.base_url}{TASK_ENDPOINT}"

    def post_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request_json("POST", TASK_ENDPOINT, payload)

    def get_json(self, path: str) -> dict[str, Any]:
        return self._request_json("GET", path, None)

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except TimeoutError as exc:
            raise BackendTimeout("backend_timeout") from exc
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise BackendHTTPError(exc.code, raw) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise BackendTimeout("backend_timeout") from exc
            raise BackendUnavailable(f"backend_unreachable: {reason}") from exc
        if not raw.strip():
            raise RalfTerminalError("empty_response")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RalfTerminalError(f"invalid_json: {exc}") from exc
        if not isinstance(data, dict):
            return {"payload": data}
        return data


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def sanitize_json(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if any(marker in str(key).lower() for marker in SECRET_MARKERS):
                out[key] = "[REDACTED]"
            else:
                out[key] = sanitize_json(item)
        return out
    if isinstance(value, list):
        return [sanitize_json(item) for item in value]
    return value


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
    found = _find_approval_request(payload)
    if found:
        return found
    if isinstance(payload, dict) and payload.get("pending_confirmation_id"):
        return {
            "action": "external_action",
            "request_id": str(payload.get("pending_confirmation_id")),
            "digest": "",
            "status": str(payload.get("stop_reason") or "human_confirmation_required"),
        }
    return None


def _find_approval_request(payload: Any) -> dict[str, str] | None:
    if isinstance(payload, dict):
        request_id = payload.get("request_id") or payload.get("approval_request_id")
        digest = payload.get("scope_digest_short") or payload.get("digest") or payload.get("scope_digest")
        action = payload.get("action")
        status = payload.get("status")
        if request_id and (digest or action or status):
            return {
                "action": str(action or payload.get("approval_action") or ""),
                "request_id": str(request_id),
                "digest": str(digest or ""),
                "status": str(status or "pending"),
            }
        for key in ("approval", "approval_request", "domain_approval", "telegram_approval", "request", "human_approval"):
            if key in payload:
                found = _find_approval_request(payload[key])
                if found:
                    return found
        for value in payload.values():
            found = _find_approval_request(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_approval_request(item)
            if found:
                return found
    return None


def format_approval_notice(approval: dict[str, str]) -> str:
    digest = approval.get("digest") or "(non disponibile)"
    return "\n".join(
        [
            "",
            "APPROVAZIONE RICHIESTA",
            f"Azione: {approval.get('action') or '(non specificata)'}",
            f"Request ID: {approval.get('request_id')}",
            f"Digest: {digest}",
            f"Stato: {approval.get('status') or 'pending'}",
            "",
            "Apri Telegram e rispondi direttamente al messaggio:",
            "  approvo",
            "oppure:",
            "  rifiuto MOTIVO",
        ]
    )


def build_task_payload(message: str, session: ChatSession, *, no_history: bool = False) -> dict[str, Any]:
    extra_context: dict[str, Any] = {
        "terminal_client": {
            "source": "ralf_terminal",
            "approval_gate": "telegram_required_for_protected_actions",
            "auto_execute_protected_actions": False,
        }
    }
    if not no_history and session.turns:
        extra_context["terminal_client"]["conversation_history"] = session.compact()
    return {"user_goal": message, "extra_context": extra_context}


def run_ask(args: argparse.Namespace, *, client: RalfHTTPClient | None = None, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    config = config_from_args(args)
    session = ChatSession(history_limit=config.history_limit)
    text = " ".join(args.message).strip()
    if not text:
        print("missing_message", file=err)
        return 2
    client = client or RalfHTTPClient(config.base_url, config.timeout)
    try:
        payload = client.post_task(build_task_payload(text, session, no_history=config.no_history))
    except RalfTerminalError as exc:
        print(str(exc), file=err)
        return 1
    return emit_response(payload, raw=config.raw, json_output=config.json_output, out=out)


def emit_response(payload: dict[str, Any], *, raw: bool = False, json_output: bool = False, out: TextIO = sys.stdout) -> int:
    safe_payload = sanitize_json(payload)
    approval = find_approval_request(payload)
    if json_output:
        if approval:
            safe_payload = {**safe_payload, "terminal_approval": approval}
        print(json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True), file=out)
        return 0
    if raw:
        print(json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True), file=out)
    else:
        print(extract_text_response(payload), file=out)
    if approval:
        print(format_approval_notice(approval), file=out)
    return 0


def run_chat(
    args: argparse.Namespace,
    *,
    client: RalfHTTPClient | None = None,
    input_func: Callable[[str], str] = input,
    out: TextIO = sys.stdout,
    err: TextIO = sys.stderr,
) -> int:
    config = config_from_args(args)
    session = ChatSession(history_limit=config.history_limit)
    raw = config.raw
    client = client or RalfHTTPClient(config.base_url, config.timeout)
    print("Ralf terminale", file=out)
    print(f"Backend: {config.base_url}", file=out)
    print("Digita /help per i comandi.", file=out)
    while True:
        try:
            message = input_func("Tu> ")
        except EOFError:
            print("", file=out)
            return 0
        except KeyboardInterrupt:
            print("\ninterrotto", file=out)
            continue
        message = message.strip()
        if not message:
            continue
        if message in {"/exit", "/quit"}:
            return 0
        if message == "/help":
            print(_help_text(), file=out)
            continue
        if message == "/reset":
            session.reset()
            print("sessione locale azzerata", file=out)
            continue
        if message == "/raw":
            raw = not raw
            print(f"raw={'on' if raw else 'off'}", file=out)
            continue
        if message == "/history":
            print(_history_text(session), file=out)
            continue
        if message == "/endpoint":
            print(_endpoint_text(config), file=out)
            continue
        if message == "/status":
            print(_status_text(client), file=out)
            continue
        try:
            payload = client.post_task(build_task_payload(message, session, no_history=config.no_history))
        except KeyboardInterrupt:
            print("\nrichiesta interrotta", file=out)
            continue
        except RalfTerminalError as exc:
            print(str(exc), file=err)
            continue
        answer = extract_text_response(payload)
        if raw:
            print(json.dumps(sanitize_json(payload), ensure_ascii=False, indent=2, sort_keys=True), file=out)
        else:
            print(f"Ralf> {answer}", file=out)
        approval = find_approval_request(payload)
        if approval:
            print(format_approval_notice(approval), file=out)
        session.add(message, answer)


def _help_text() -> str:
    return "\n".join(
        [
            "/help      mostra comandi",
            "/reset     azzera cronologia locale",
            "/status    verifica backend senza privilegi",
            "/history   mostra cronologia sintetica",
            "/raw       attiva/disattiva JSON completo",
            "/endpoint  mostra endpoint e schema usati",
            "/exit      esci",
            "/quit      esci",
        ]
    )


def _history_text(session: ChatSession) -> str:
    if not session.turns:
        return "(cronologia vuota)"
    rows = []
    for idx, turn in enumerate(session.turns, start=1):
        rows.append(f"{idx}. Tu: {turn.user[:120]}")
        rows.append(f"   Ralf: {turn.ralf[:120]}")
    return "\n".join(rows)


def _endpoint_text(config: ChatConfig) -> str:
    return json.dumps(
        {
            "endpoint": f"{config.base_url}{TASK_ENDPOINT}",
            "method": "POST",
            "request_schema": {
                "user_goal": "string",
                "mode": "optional string",
                "skill_context": "optional string",
                "extra_context": "optional object",
            },
            "response_text_keys": list(TEXT_KEYS),
            "session": "local_history",
        },
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )


def _status_text(client: RalfHTTPClient) -> str:
    status: dict[str, Any] = {"backend": client.base_url, "tasks_run": f"{client.base_url}{TASK_ENDPOINT}"}
    try:
        openapi = client.get_json("/openapi.json")
        status["openapi"] = "ok"
        status["tasks_run_available"] = TASK_ENDPOINT in openapi.get("paths", {})
    except RalfTerminalError as exc:
        status["openapi"] = str(exc)
        status["tasks_run_available"] = False
    try:
        approval = client.get_json("/domain-approvals/health")
        status["domain_approval_health"] = sanitize_json(approval)
    except RalfTerminalError as exc:
        status["domain_approval_health"] = str(exc)
    return json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True)


def config_from_args(args: argparse.Namespace) -> ChatConfig:
    config = ChatConfig.from_env()
    if getattr(args, "base_url", None):
        config.base_url = args.base_url.rstrip("/")
    if getattr(args, "timeout", None) is not None:
        config.timeout = float(args.timeout)
    config.raw = bool(getattr(args, "raw", False))
    config.json_output = bool(getattr(args, "json", False))
    config.no_history = bool(getattr(args, "no_history", False))
    return config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ralf", description="Terminal client for Ralfloop")
    parser.add_argument("--base-url", default=None, help=f"backend URL (default: {DEFAULT_BASE_URL})")
    parser.add_argument("--timeout", type=float, default=None, help="request timeout seconds")
    parser.add_argument("--raw", action="store_true", help="print full redacted JSON")
    sub = parser.add_subparsers(dest="command")

    chat = sub.add_parser("chat", help="start interactive chat")
    chat.add_argument("--base-url", default=None, help=f"backend URL (default: {DEFAULT_BASE_URL})")
    chat.add_argument("--timeout", type=float, default=None, help="request timeout seconds")
    chat.add_argument("--raw", action="store_true", help="start with full JSON enabled")
    chat.add_argument("--no-history", action="store_true", help="do not send local history in extra_context")

    ask = sub.add_parser("ask", help="send one request")
    ask.add_argument("message", nargs="+", help="message for Ralf")
    ask.add_argument("--raw", action="store_true", help="print full redacted JSON")
    ask.add_argument("--json", action="store_true", help="print full redacted JSON")
    ask.add_argument("--base-url", default=None, help=f"backend URL (default: {DEFAULT_BASE_URL})")
    ask.add_argument("--timeout", type=float, default=None, help="request timeout seconds")
    ask.add_argument("--no-history", action="store_true", help="do not send local history in extra_context")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if not argv:
        args = argparse.Namespace(command="chat", base_url=None, timeout=None, raw=False, no_history=False)
        return run_chat(args)
    args = parser.parse_args(argv)
    if args.command in {None, "chat"}:
        return run_chat(args)
    if args.command == "ask":
        return run_ask(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
