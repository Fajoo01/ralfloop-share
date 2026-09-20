from __future__ import annotations

"""Bot-tazzi MD/Goodify helpers.

The module deliberately exposes only bounded semantic operations: decode a QR,
resolve an allow-listed public URL, watch Goodify/MD mail, forward it to one
configured address, and emit a Telegram notification for an already reported
win. It never exposes a generic browser executor and never plays an instant-win
entry.
"""

from dataclasses import dataclass
from email.utils import parseaddr
from html.parser import HTMLParser
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Mapping
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from src.mcp_transport import MCPClientSession, UnixMCPTransport

ALLOWED_HOST_SUFFIXES = ("goodify.com", "mdspa.it")
ALLOWED_EMAIL_DOMAINS = ("goodify.com", "mdspa.it")
DEFAULT_NONPROFIT = os.getenv("RALFLOOP_MD_GOODIFY_NONPROFIT", "Tiremm Innanz APS")
DEFAULT_FORWARD_TO = os.getenv("RALFLOOP_MD_GOODIFY_FORWARD_TO", "fabio@tiremminnanz.com")
DEFAULT_QR_DIR = Path("/var/lib/ralfloop/md-goodify/qr-inbox")
DEFAULT_STATE_PATH = Path("/var/lib/ralfloop/md-goodify/state.json")
DEFAULT_TELEGRAM_OUTBOX = Path("/var/lib/ralfloop/domain-approval-outbox.jsonl")


def _allowed_host(host: str) -> bool:
    host = host.casefold().rstrip(".")
    return any(host == suffix or host.endswith("." + suffix) for suffix in ALLOWED_HOST_SUFFIXES)


def _safe_https_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme.casefold() != "https" or not parsed.hostname or not _allowed_host(parsed.hostname):
        raise ValueError("md_goodify_url_not_allowlisted")
    if parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("md_goodify_url_unsafe_authority")
    return parsed.geturl()


def _first_url(text: str) -> str:
    match = re.search(r"https?://[^\s<>\"']+", text or "", re.I)
    if not match:
        raise ValueError("md_goodify_qr_missing_https_url")
    return _safe_https_url(match.group(0).rstrip(".,);]"))


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MdGoodifyQr:
    payload: str
    url: str
    host: str
    token_hint: str
    fingerprint: str


def parse_md_goodify_qr(payload: str) -> MdGoodifyQr:
    raw = " ".join(str(payload or "").split())
    if not raw or len(raw) > 4096:
        raise ValueError("md_goodify_qr_invalid_payload")
    url = _first_url(raw)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    token = ""
    for key in ("code", "token", "id", "qr", "voucher", "campaign"):
        values = query.get(key) or ()
        if values and str(values[0]).strip():
            token = str(values[0]).strip()[:160]
            break
    if not token:
        tail = parsed.path.rstrip("/").rsplit("/", 1)[-1]
        token = tail[:160] if tail and tail not in {"goodify", "md"} else ""
    return MdGoodifyQr(
        payload=raw,
        url=url,
        host=str(parsed.hostname or "").casefold(),
        token_hint=token,
        fingerprint=_fingerprint(raw),
    )


def decode_qr_image(image_path: str, *, qr_root: str | Path | None = None) -> MdGoodifyQr:
    root = Path(qr_root or os.getenv("RALFLOOP_MD_GOODIFY_QR_DIR") or DEFAULT_QR_DIR).resolve()
    path = Path(image_path).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("md_goodify_qr_path_outside_spool") from exc
    if not path.is_file():
        raise ValueError("md_goodify_qr_image_missing")
    proc = subprocess.run(
        ["/usr/bin/zbarimg", "--quiet", "--raw", str(path)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if proc.returncode not in (0, 4) or not proc.stdout.strip():
        raise ValueError("md_goodify_qr_decode_failed")
    candidates = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    errors: list[str] = []
    for candidate in candidates:
        try:
            return parse_md_goodify_qr(candidate)
        except ValueError as exc:
            errors.append(str(exc))
    raise ValueError(errors[-1] if errors else "md_goodify_qr_not_supported")


class _FormProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.casefold() == "form":
            self._current = {"action": values.get("action") or "", "method": (values.get("method") or "get").casefold(), "fields": []}
            self.forms.append(self._current)
        elif self._current is not None and tag.casefold() in {"input", "select", "textarea", "button"}:
            name = str(values.get("name") or "").strip()
            if name:
                self._current["fields"].append(name)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "form":
            self._current = None


class _AllowlistedRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return super().redirect_request(req, fp, code, msg, headers, _safe_https_url(newurl))


def probe_public_flow(qr: MdGoodifyQr, *, timeout: float = 10.0) -> dict[str, Any]:
    opener = build_opener(_AllowlistedRedirect())
    request = Request(qr.url, headers={"User-Agent": "Bot-tazzi-md-goodify/1.0", "Accept": "text/html,application/xhtml+xml"})
    with opener.open(request, timeout=timeout) as response:
        final_url = _safe_https_url(response.geturl())
        content_type = str(response.headers.get("Content-Type") or "")
        body = response.read(512 * 1024)
    text = body.decode("utf-8", errors="replace") if "html" in content_type.casefold() else ""
    parser = _FormProbe()
    if text:
        parser.feed(text)
    forms = []
    for form in parser.forms[:12]:
        action = str(form.get("action") or "").strip()
        try:
            resolved_action = _safe_https_url(urljoin(final_url, action)) if action else final_url
        except ValueError:
            continue
        forms.append({
            "action": resolved_action,
            "method": form.get("method"),
            "fields": list(dict.fromkeys(form.get("fields") or ()))[:40],
        })
    return {
        "status": "PUBLIC_FLOW_DISCOVERED",
        "qr_fingerprint": qr.fingerprint,
        "initial_url": qr.url,
        "final_url": final_url,
        "forms": forms,
        "mentions_tiremm": "tiremm" in text.casefold(),
        "mentions_nonprofit": any(word in text.casefold() for word in ("non profit", "nonprofit", "associazione")),
        "side_effects": 0,
    }


WIN_PATTERNS = (
    re.compile(r"\bcomplimenti\b.{0,120}\bhai\s+vinto\b", re.I | re.S),
    re.compile(r"\bhai\s+vinto\b.{0,120}\b(?:premio|voucher|gift\s*card|€|euro)\b", re.I | re.S),
    re.compile(r"\bvincita\s+(?:confermata|assegnata)\b", re.I),
)
LOSS_PATTERNS = (
    re.compile(r"\bnon\s+hai\s+vinto\b", re.I),
    re.compile(r"\bnessun\s+premio\b", re.I),
    re.compile(r"\bgiocata\s+non\s+vincente\b", re.I),
)
AMOUNT_RE = re.compile(r"(?:€\s*|\bEUR\s*)(\d{1,4}(?:[.,]\d{1,2})?)|(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|euro)\b", re.I)


def classify_goodify_outcome(subject: str, body: str) -> dict[str, Any]:
    text = " ".join(f"{subject}\n{body}".split())
    if any(pattern.search(text) for pattern in LOSS_PATTERNS):
        status = "LOSS"
    elif any(pattern.search(text) for pattern in WIN_PATTERNS):
        status = "WIN"
    else:
        status = "UNKNOWN"
    amounts = []
    for match in AMOUNT_RE.finditer(text):
        raw = next((group for group in match.groups() if group), "")
        if raw:
            amounts.append(raw.replace(",", "."))
    return {
        "status": status,
        "amounts_eur": list(dict.fromkeys(amounts))[:5],
        "evidence_sha256": _fingerprint(text),
    }


def _sender_allowed(value: str) -> bool:
    address = parseaddr(value)[1].casefold()
    if "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1]
    return any(domain == suffix or domain.endswith("." + suffix) for suffix in ALLOWED_EMAIL_DOMAINS)


def _load_state(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        value = {}
    return value if isinstance(value, dict) else {}


def _save_state(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(path)


def _append_telegram(message_id: str, message: str, *, outbox_path: Path) -> str:
    request_id = "mdgoodify_" + _fingerprint(message_id)[:24]
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "status": "queued",
        "kind": "md_goodify_win_notification",
        "request_id": request_id,
        "created_at": int(time.time()),
        "message": message[:3900],
    }
    with outbox_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    return request_id


def _extract_message(detail: Mapping[str, Any]) -> dict[str, str]:
    row = detail.get("message") if isinstance(detail.get("message"), Mapping) else detail
    if not isinstance(row, Mapping):
        raise ValueError("goodify_email_invalid_message")
    return {
        "message_id": str(row.get("messageId") or row.get("message_id") or row.get("id") or ""),
        "sender": str(row.get("from") or row.get("sender") or ""),
        "subject": str(row.get("subject") or ""),
        "date": str(row.get("date") or ""),
        "body": str(row.get("body") or ""),
    }


def process_goodify_mailbox(
    *,
    account: str,
    forward_to: str = DEFAULT_FORWARD_TO,
    state_path: str | Path | None = None,
    telegram_outbox: str | Path | None = None,
    gmail_socket: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    from src.google_workspace import GoogleWorkspaceGateway

    state_file = Path(state_path or os.getenv("RALFLOOP_MD_GOODIFY_STATE") or DEFAULT_STATE_PATH)
    outbox = Path(telegram_outbox or os.getenv("RALFLOOP_TELEGRAM_APPROVAL_OUTBOX") or DEFAULT_TELEGRAM_OUTBOX)
    socket_path = gmail_socket or os.getenv("RALF_GOOGLE_WORKSPACE_MCP_SOCKET", "/run/ralf-google-workspace-mcp/mcp.sock")
    state = _load_state(state_file)
    processed = set(map(str, state.get("processed_message_ids") or ()))
    events: list[dict[str, Any]] = []
    query = "newer_than:30d {from:(goodify.com) from:(mdspa.it)}"

    session = MCPClientSession(UnixMCPTransport(socket_path), timeout=20, client_name="bot-tazzi-md-goodify")
    try:
        session.initialize()
        gateway = GoogleWorkspaceGateway(session, account=account)
        gateway.discover()
        search = gateway.invoke("search", query=query, maxResults=min(50, max(1, limit)))
        rows = search.get("messages") if isinstance(search.get("messages"), list) else []
        for candidate in reversed(rows):
            message_id = str(candidate.get("messageId") or candidate.get("message_id") or candidate.get("id") or "")
            if not message_id or message_id in processed:
                continue
            detail = gateway.invoke("read", messageId=message_id)
            message = _extract_message(detail)
            if not _sender_allowed(message["sender"]):
                continue
            outcome = classify_goodify_outcome(message["subject"], message["body"])
            forwarded = False
            forward_status = "not_requested"
            if forward_to:
                if account.casefold() == forward_to.casefold():
                    forward_status = "same_account_skip"
                else:
                    session.call_tool("manage_email", {
                        "operation": "forward",
                        "email": account,
                        "messageId": message_id,
                        "to": forward_to,
                        "body": "Inoltro automatico Bot-tazzi MD/Goodify.",
                    })
                    forwarded = True
                    forward_status = "forwarded"
            notification_id = ""
            if outcome["status"] == "WIN":
                amount = ", ".join(f"€ {value}" for value in outcome["amounts_eur"])
                suffix = f" Importo rilevato: {amount}." if amount else ""
                notification_id = _append_telegram(
                    message_id,
                    f"Bot-tazzi — Goodify: vincita rilevata. Oggetto: {message['subject']}.{suffix}",
                    outbox_path=outbox,
                )
            processed.add(message_id)
            events.append({
                "message_id": message_id,
                "sender": message["sender"],
                "subject": message["subject"],
                "outcome": outcome,
                "forwarded": forwarded,
                "forward_status": forward_status,
                "telegram_request_id": notification_id,
            })
    finally:
        session.close()

    state["processed_message_ids"] = sorted(processed)[-2000:]
    state["updated_at"] = int(time.time())
    _save_state(state_file, state)
    return {
        "ok": True,
        "account": account,
        "forward_to": forward_to,
        "processed_now": len(events),
        "wins": sum(item["outcome"]["status"] == "WIN" for item in events),
        "events": events,
    }
