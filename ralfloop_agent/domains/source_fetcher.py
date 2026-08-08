from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import socket
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path


ALLOWED_MIME = {
    "application/pdf",
    "text/html",
    "text/plain",
    "application/json",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
BLOCKED_SUFFIXES = (".exe", ".sh", ".bat", ".cmd", ".msi", ".dmg", ".zip", ".tar", ".gz", ".7z")


@dataclass
class FetchedSource:
    ok: bool
    status: str
    url: str
    final_url: str = ""
    redirect_chain: list[str] = field(default_factory=list)
    content_type: str = ""
    checksum: str = ""
    size_bytes: int = 0
    cache_path: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class SourceFetcher:
    def __init__(self, *, cache_dir: str | Path = ".ralf_run/bando_web_cache", timeout_sec: float = 30.0, max_bytes: int = 52_428_800, user_agent: str = "Ralfloop-BandoResearch/1.0") -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout_sec = timeout_sec
        self.max_bytes = max_bytes
        self.user_agent = user_agent

    def fetch(self, url: str) -> FetchedSource:
        blocked = validate_url(url)
        if blocked:
            return FetchedSource(False, blocked, url, error=blocked)
        if url.lower().split("?")[0].endswith(BLOCKED_SUFFIXES):
            return FetchedSource(False, "executable_or_archive_blocked", url, error="blocked_suffix")
        redirect_handler = _TrackingRedirectHandler()
        opener = urllib.request.build_opener(redirect_handler)
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": ", ".join(sorted(ALLOWED_MIME))})
        try:
            with opener.open(req, timeout=self.timeout_sec) as resp:
                final_url = resp.geturl()
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in ALLOWED_MIME:
                    return FetchedSource(False, "mime_not_allowed", url, final_url=final_url, content_type=ctype, error="mime_not_allowed")
                content = _read_limited(resp, self.max_bytes)
        except ValueError as exc:
            return FetchedSource(False, "file_too_large", url, error=str(exc))
        except Exception as exc:
            return FetchedSource(False, "fetch_error", url, error=f"{type(exc).__name__}: {exc}")
        checksum = hashlib.sha256(content).hexdigest()
        cache_path = self._write_cache(checksum, content, url, final_url, ctype, redirect_handler.redirect_chain, dict(resp.headers.items()))
        return FetchedSource(True, "fetched", url, final_url, list(redirect_handler.redirect_chain), ctype, checksum, len(content), str(cache_path))

    def _write_cache(self, checksum: str, content: bytes, url: str, final_url: str, content_type: str, redirects: list[str], headers: dict[str, str]) -> Path:
        path = self.cache_dir / checksum
        path.mkdir(parents=True, exist_ok=True)
        (path / "content").write_bytes(content)
        metadata = {
            "url": url,
            "final_url": final_url,
            "content_type": content_type,
            "checksum": checksum,
            "size_bytes": len(content),
            "retrieved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "redirect_chain": redirects,
        }
        (path / "metadata.json").write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        redacted = {k: v for k, v in headers.items() if k.lower() not in {"authorization", "cookie", "set-cookie"}}
        (path / "headers-redacted.json").write_text(json.dumps(redacted, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (path / "assessment.json").write_text("{}\n", encoding="utf-8")
        (path / "provenance.json").write_text(json.dumps({"source": "web_fetch", "url": url, "final_url": final_url}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path


def validate_url(url: str) -> str | None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "scheme_not_allowed"
    host = parsed.hostname
    if not host:
        return "host_missing"
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return "dns_resolution_failed"
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            return "ssrf_blocked"
    return None


def _read_limited(resp, max_bytes: int) -> bytes:
    chunks = []
    total = 0
    while True:
        chunk = resp.read(min(65536, max_bytes + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("file_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


class _TrackingRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self) -> None:
        super().__init__()
        self.redirect_chain: list[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        blocked = validate_url(newurl)
        if blocked:
            raise RuntimeError(blocked)
        self.redirect_chain.append(newurl)
        if len(self.redirect_chain) > 5:
            raise RuntimeError("too_many_redirects")
        return super().redirect_request(req, fp, code, msg, headers, newurl)
