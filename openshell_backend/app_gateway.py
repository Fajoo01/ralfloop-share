from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import subprocess
import tempfile
import time
from typing import Any
from urllib.parse import unquote_plus

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
import requests

from openshell_backend import oidc_auth
from ralfloop_agent.call_recordings import CallRecordingStore

APP_NAME = "Bot-tazzi — App"
UI_PATH = Path(__file__).with_name("bottazzi_ui.html")
BACKEND = os.getenv("BOTTAZZI_APP_BACKEND", "http://127.0.0.1:19090").rstrip("/")
SCHOLARLY_BACKEND = os.getenv("BOTTAZZI_APP_SCHOLARLY_BACKEND", "").rstrip("/")
UPLOAD_ROOT = Path(os.getenv("BOTTAZZI_APP_UPLOAD_ROOT", "/var/lib/ralfloop-bottazzi-call-recordings/app-uploads"))
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
COOKIE = "bottazzi_app_session"
OIDC_STATE_COOKIE = "bottazzi_oidc_state"
SESSION_TTL = int(os.getenv("BOTTAZZI_APP_SESSION_TTL", "86400"))
OIDC_STATE_TTL = 600
PUBLIC_PATHS = {"/login", "/oidc/login", "/oidc/callback", "/healthz", "/manifest.webmanifest", "/sw.js", "/icon.svg"}

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
CALL_RECORDINGS = CallRecordingStore.from_env()


def _prefix(request: Request) -> str:
    value = request.headers.get("x-forwarded-prefix", "").strip()
    if not value:
        return ""
    if not value.startswith("/"):
        value = "/" + value
    return value.rstrip("/")


def _url(request: Request, path: str) -> str:
    return f"{_prefix(request)}{path}"


def _password_ok(candidate: str) -> bool:
    spec = os.getenv("BOTTAZZI_APP_PASSWORD_HASH", "").strip()
    try:
        algorithm, iterations, salt_hex, expected_hex = spec.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        derived = hashlib.pbkdf2_hmac(
            "sha256", candidate.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
        ).hex()
    except (TypeError, ValueError):
        return False
    return hmac.compare_digest(derived, expected_hex)


def _session_secret() -> bytes:
    return os.getenv("BOTTAZZI_APP_SESSION_SECRET", "").encode("utf-8")


def _make_session() -> str:
    now = str(int(time.time()))
    signature = hmac.new(_session_secret(), now.encode("ascii"), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{now}.{signature}".encode("ascii")).decode("ascii")


def _session_ok(token: str | None) -> bool:
    if not token or not _session_secret():
        return False
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("ascii")
        stamp_s, signature = raw.split(".", 1)
        stamp = int(stamp_s)
    except (ValueError, UnicodeError):
        return False
    now = int(time.time())
    if stamp > now + 60 or now - stamp > SESSION_TTL:
        return False
    expected = hmac.new(_session_secret(), stamp_s.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _secure_cookie(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return proto.casefold() == "https"


@app.middleware("http")
async def app_auth(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    if _session_ok(request.cookies.get(COOKIE)):
        return await call_next(request)
    if "text/html" in request.headers.get("accept", ""):
        target = "/oidc/login" if oidc_auth.enabled() else "/login"
        return RedirectResponse(_url(request, target), status_code=303)
    return JSONResponse({"detail": "app_auth_required"}, status_code=401)


LOGIN_HTML = """<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'><meta name='theme-color' content='#171512'><title>Bot-tazzi — Accesso privato</title><style>:root{color-scheme:dark;font-family:Inter,system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;place-items:center;background:radial-gradient(circle at 80% 0,#38251f 0,transparent 30%),#171512;color:#f3ead8}.card{width:min(430px,calc(100vw - 32px));background:#211e1a;border:1px solid #3c362e;border-radius:22px;padding:28px;box-shadow:0 22px 60px #0008}.mark{width:72px;height:72px;border-radius:22px;display:grid;place-items:center;background:linear-gradient(145deg,#f0d3aa,#b87d51);overflow:hidden}.mark svg{width:70px;height:70px}h1{margin:18px 0 8px;font-size:30px}p{color:#b6aa96;line-height:1.5}input,button{width:100%;font:inherit;border-radius:12px}input{margin-top:12px;padding:14px;background:#171512;border:1px solid #4a4238;color:#f3ead8;outline:none}button{margin-top:12px;padding:13px;border:0;background:#e34b3d;color:white;font-weight:800;cursor:pointer}.err{min-height:20px;color:#ef9389;font-size:13px;margin-top:10px}</style></head><body><main class='card'><div class='mark'><svg viewBox='0 0 100 100' aria-label='Peppone'><path d='M20 30Q50 8 80 30L76 40H24Z' fill='#472b22'/><circle cx='50' cy='55' r='27' fill='#d8a06f'/><path d='M26 44Q50 30 74 44' fill='none' stroke='#472b22' stroke-width='5'/><circle cx='40' cy='52' r='3' fill='#241812'/><circle cx='60' cy='52' r='3' fill='#241812'/><path d='M50 57l-3 8h6' fill='none' stroke='#8c5a37' stroke-width='2'/><path d='M49 67c-7-8-15-5-18 0 6 2 12 3 18 1 6 2 12 1 18-1-3-5-11-8-18 0z' fill='#2a1a15'/><path d='M37 76q13 8 26 0' fill='none' stroke='#7d3f2f' stroke-width='2'/></svg></div><h1>Bot-tazzi</h1><p>Peppone · assistente locale. Accesso privato alla tua infrastruttura.</p><form id='f'><input id='p' type='password' autocomplete='current-password' autofocus placeholder='Password Bot-tazzi'><button>Entra</button><div class='err' id='e'></div></form></main><script>document.querySelector('#f').addEventListener('submit',async e=>{e.preventDefault();const out=document.querySelector('#e');out.textContent='';const r=await fetch('./login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:document.querySelector('#p').value})});const j=await r.json().catch(()=>({}));if(r.ok){location.href=j.next||'./'}else out.textContent='Password errata';});</script></body></html>"""


@app.get("/oidc/login")
def oidc_login(request: Request) -> Response:
    if not oidc_auth.enabled():
        return RedirectResponse(_url(request, "/login"), status_code=303)
    state = secrets.token_urlsafe(32)
    try:
        target = oidc_auth.authorization_url(state=state)
    except (RuntimeError, requests.RequestException, ValueError):
        return JSONResponse({"detail": "portachiavi_unavailable"}, status_code=503)
    response = RedirectResponse(target, status_code=303)
    prefix = _prefix(request)
    response.set_cookie(
        OIDC_STATE_COOKIE,
        state,
        max_age=OIDC_STATE_TTL,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path=(prefix + "/") if prefix else "/",
    )
    return response


@app.get("/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "") -> Response:
    expected = request.cookies.get(OIDC_STATE_COOKIE)
    if error or not code or not state or not expected or not hmac.compare_digest(expected, state):
        return JSONResponse({"detail": "portachiavi_callback_invalid"}, status_code=400)
    try:
        oidc_auth.exchange_code(code)
    except (RuntimeError, requests.RequestException, ValueError):
        return JSONResponse({"detail": "portachiavi_login_failed"}, status_code=503)
    prefix = _prefix(request)
    response = RedirectResponse(_url(request, "/"), status_code=303)
    response.set_cookie(
        COOKIE,
        _make_session(),
        max_age=SESSION_TTL,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path=(prefix + "/") if prefix else "/",
    )
    response.delete_cookie(OIDC_STATE_COOKIE, path=(prefix + "/") if prefix else "/")
    return response


@app.get("/login")
def login_page(request: Request) -> Response:
    if oidc_auth.enabled():
        return RedirectResponse(_url(request, "/oidc/login"), status_code=303)
    return HTMLResponse(LOGIN_HTML, headers={"cache-control": "no-store"})


@app.post("/login")
async def login(request: Request) -> JSONResponse:
    if oidc_auth.enabled():
        return JSONResponse({"detail": "password_login_disabled"}, status_code=410)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not _password_ok(str(payload.get("password") or "")):
        return JSONResponse({"ok": False}, status_code=401)
    response = JSONResponse({"ok": True, "next": _url(request, "/")})
    prefix = _prefix(request)
    response.set_cookie(
        COOKIE,
        _make_session(),
        max_age=SESSION_TTL,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="lax",
        path=(prefix + "/") if prefix else "/",
    )
    return response


@app.post("/logout")
def logout(request: Request) -> JSONResponse:
    response = JSONResponse({"ok": True, "next": _url(request, "/login")})
    prefix = _prefix(request)
    response.delete_cookie(COOKIE, path=(prefix + "/") if prefix else "/")
    return response


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "bottazzi-app-gateway",
        "call_recordings": True,
        "auth_mode": oidc_auth.auth_mode(),
    }


def _ui() -> HTMLResponse:
    html = UI_PATH.read_text(encoding="utf-8")
    html = html.replace("<body>", '<body data-app-gateway="1">', 1)
    return HTMLResponse(html, headers={"cache-control": "no-store"})


@app.get("/", response_class=HTMLResponse)
def root_ui() -> HTMLResponse:
    return _ui()


@app.get("/assistant/v1", response_class=HTMLResponse)
@app.get("/assistant/v1/ui", response_class=HTMLResponse)
def assistant_ui() -> HTMLResponse:
    return _ui()


def _proxy_response(upstream: requests.Response) -> Response:
    media_type = upstream.headers.get("content-type", "application/json").split(";", 1)[0]
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media_type)


@app.get("/assistant/v1/status")
def assistant_status() -> Response:
    try:
        upstream = requests.get(f"{BACKEND}/assistant/v1/status", timeout=5)
    except requests.RequestException:
        return JSONResponse({"ok": False, "detail": "assistant_backend_unavailable"}, status_code=503)
    if upstream.ok:
        try:
            payload = upstream.json()
            payload.update({"app_gateway": True, "app_private": True, "app_modules": ["local_chat", "internet_agent", "scholarly", "tools", "fast", "deep"]})
            return JSONResponse(payload)
        except ValueError:
            pass
    return _proxy_response(upstream)


@app.api_route("/assistant/v1/tasks", methods=["GET", "POST"])
@app.api_route(
    "/assistant/v1/tasks/{rest_of_path:path}",
    methods=["GET", "POST", "PATCH", "DELETE"],
)
async def assistant_tasks(request: Request, rest_of_path: str = "") -> Response:
    suffix = f"/{rest_of_path}" if rest_of_path else ""
    headers = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    try:
        upstream = requests.request(
            request.method,
            f"{BACKEND}/assistant/v1/tasks{suffix}",
            params=list(request.query_params.multi_items()),
            data=await request.body(),
            headers=headers,
            timeout=10,
        )
    except requests.RequestException:
        return JSONResponse({"detail": "assistant_backend_unavailable"}, status_code=503)
    return _proxy_response(upstream)


@app.api_route("/assistant/v1/call-recordings", methods=["GET", "POST"])
async def assistant_call_recordings(request: Request) -> Response:
    if request.method == "GET":
        try:
            limit = min(max(int(request.query_params.get("limit", "50")), 1), 200)
        except ValueError:
            return JSONResponse({"detail": "invalid_limit"}, status_code=400)
        return JSONResponse({"ok": True, "recordings": CALL_RECORDINGS.list(limit=limit)})

    raw_length = request.headers.get("content-length", "").strip()
    if raw_length.isdigit() and int(raw_length) > CALL_RECORDINGS.max_bytes:
        return JSONResponse({"detail": "recording_too_large"}, status_code=413)
    content = await request.body()
    filename = unquote_plus(request.headers.get("x-bottazzi-filename", "call-recording"))
    extra = {
        "transport": request.headers.get("x-bottazzi-transport", "android_share"),
        "caller": request.headers.get("x-bottazzi-caller", ""),
        "direction": request.headers.get("x-bottazzi-direction", ""),
        "call_started_at": request.headers.get("x-bottazzi-call-started-at", ""),
    }
    try:
        recording = CALL_RECORDINGS.ingest(
            content,
            filename=filename,
            content_type=request.headers.get("content-type", "application/octet-stream"),
            extra=extra,
        )
    except ValueError as exc:
        detail = str(exc)
        code = 413 if detail == "recording_too_large" else 400
        return JSONResponse({"detail": detail}, status_code=code)
    return JSONResponse({"ok": True, "recording": recording}, status_code=201)


@app.api_route("/assistant/v1/accounting/review", methods=["GET"])
@app.api_route(
    "/assistant/v1/accounting/review/{rest_of_path:path}",
    methods=["GET", "POST"],
)
async def assistant_accounting_review(request: Request, rest_of_path: str = "") -> Response:
    suffix = f"/{rest_of_path}" if rest_of_path else ""
    headers = {}
    content_type = request.headers.get("content-type")
    if content_type:
        headers["content-type"] = content_type
    try:
        upstream = requests.request(
            request.method,
            f"{BACKEND}/assistant/v1/accounting/review{suffix}",
            params=list(request.query_params.multi_items()),
            data=await request.body(),
            headers=headers,
            timeout=30,
        )
    except requests.RequestException:
        return JSONResponse({"detail": "assistant_backend_unavailable"}, status_code=503)
    return _proxy_response(upstream)



@app.post("/assistant/v1/audio/transcribe")
async def assistant_audio_transcribe(request: Request) -> Response:
    content = await request.body()
    if not content:
        return JSONResponse({"detail": "audio_required"}, status_code=400)
    if len(content) > 8 * 1024 * 1024:
        return JSONResponse({"detail": "audio_too_large"}, status_code=413)
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    suffixes = {
        "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/mp4": ".m4a",
        "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
        "application/octet-stream": ".webm",
    }
    suffix = suffixes.get(mime)
    if suffix is None:
        return JSONResponse({"detail": "audio_type_not_supported"}, status_code=415)
    python_bin = os.getenv("BOTTAZZI_WHISPER_PYTHON", "/home/sibilla-cumana/venvs/asr/bin/python")
    model_name = os.getenv("BOTTAZZI_WHISPER_MODEL", "small").strip() or "small"
    language = os.getenv("BOTTAZZI_WHISPER_LANGUAGE", "it").strip() or "it"
    code = (
        "import sys; from faster_whisper import WhisperModel; "
        "m=WhisperModel(sys.argv[2],device='cpu',compute_type='int8'); "
        "s,_=m.transcribe(sys.argv[1],beam_size=5,vad_filter=True,language=sys.argv[3]); "
        "print(' '.join(x.text.strip() for x in s).strip())"
    )
    try:
        with tempfile.NamedTemporaryFile(prefix="bottazzi_app_voice_", suffix=suffix) as handle:
            handle.write(content)
            handle.flush()
            cp = subprocess.run(
                [python_bin, "-c", code, handle.name, model_name, language],
                capture_output=True, text=True, timeout=90, stdin=subprocess.DEVNULL,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return JSONResponse({"detail": "audio_transcription_failed"}, status_code=503)
    if cp.returncode != 0:
        return JSONResponse({"detail": "audio_transcription_failed"}, status_code=503)
    text = (cp.stdout or "").strip()
    if not text:
        return JSONResponse({"detail": "audio_not_understood"}, status_code=422)
    return JSONResponse({"ok": True, "text": text[:32000], "engine": "faster-whisper", "language": language})


@app.post("/assistant/v1/attachments")
async def assistant_attachment(request: Request) -> Response:
    content = await request.body()
    if not content:
        return JSONResponse({"detail": "attachment_required"}, status_code=400)
    if len(content) > MAX_UPLOAD_BYTES:
        return JSONResponse({"detail": "attachment_too_large"}, status_code=413)
    raw_name = unquote_plus(request.headers.get("x-bottazzi-filename", "allegato")).strip()
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(raw_name).name).strip(" .")[:120] or "allegato"
    suffix = Path(safe_name).suffix.casefold()
    allowed = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".txt", ".md", ".csv", ".json", ".doc", ".docx", ".xls", ".xlsx"}
    if suffix not in allowed:
        return JSONResponse({"detail": "attachment_type_not_supported"}, status_code=415)
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    attachment_id = secrets.token_hex(12)
    path = UPLOAD_ROOT / f"{attachment_id}-{safe_name}"
    path.write_bytes(content)
    os.chmod(path, 0o600)
    return JSONResponse({"ok": True, "attachment": {
        "id": attachment_id, "name": safe_name, "path": str(path),
        "content_type": request.headers.get("content-type", "application/octet-stream").split(";", 1)[0],
        "size": len(content),
    }})


@app.post("/assistant/v1/chat")
def assistant_chat(payload: dict[str, Any]) -> Response:
    outgoing = dict(payload)
    app_attachments = outgoing.pop("app_attachments", [])
    if isinstance(app_attachments, list) and app_attachments:
        safe_rows = []
        root = UPLOAD_ROOT.resolve()
        for item in app_attachments[:5]:
            if not isinstance(item, dict):
                continue
            try:
                path = Path(str(item.get("path") or "")).resolve()
            except (OSError, RuntimeError):
                continue
            if not path.is_file() or not (path == root or root in path.parents):
                continue
            safe_rows.append({"name": str(item.get("name") or path.name)[:120], "path": str(path), "content_type": str(item.get("content_type") or "application/octet-stream")})
        if safe_rows:
            original = str(outgoing.get("message") or "").strip() or "Analizza gli allegati."
            lines = ["- {}: {} ({})".format(row["name"], row["path"], row["content_type"]) for row in safe_rows]
            outgoing["message"] = (original + "\n\nAllegati locali disponibili sul server:\n" + "\n".join(lines) + "\nLeggi e usa questi file come input; per PDF o immagini usa il percorso visual/visual RAG quando necessario.")[:32000]
            context = dict(outgoing.get("context") or {})
            context["app_attachments"] = safe_rows
            outgoing["context"] = context
    internet_agent = bool(outgoing.pop("app_internet_agent", False))
    scholarly = bool(outgoing.pop("app_scholarly", False))
    if internet_agent:
        original = str(outgoing.get("message") or "").strip()
        if original.casefold() in {"riprova", "riprovaci", "prova di nuovo", "di nuovo"}:
            history = outgoing.get("history")
            if isinstance(history, list):
                for item in reversed(history):
                    if not isinstance(item, dict) or str(item.get("role") or "") != "user":
                        continue
                    previous = str(item.get("content") or "").strip()
                    if previous:
                        original = previous
                        break
        outgoing["message"] = (
            "Ricerca approfondita. Domanda originale: " + original
        )[:32_000]
        outgoing["allow_tools"] = True
        context = dict(outgoing.get("context") or {})
        context["app_internet_agent"] = True
        outgoing["context"] = context
    target_backend = SCHOLARLY_BACKEND if scholarly else BACKEND
    if scholarly and not target_backend:
        return JSONResponse({"detail": "scholarly_backend_unavailable"}, status_code=503)
    if scholarly:
        original = str(outgoing.get("message") or "").strip()
        if original and not original.casefold().startswith("filologo:"):
            outgoing["message"] = ("Filologo: " + original)[:32_000]
        context = dict(outgoing.get("context") or {})
        context["app_scholarly"] = True
        outgoing["context"] = context
        outgoing["allow_tools"] = True
    try:
        upstream = requests.post(
            f"{target_backend}/assistant/v1/chat",
            json=outgoing,
            timeout=int(os.getenv("BOTTAZZI_APP_BACKEND_TIMEOUT", "300")),
        )
    except requests.RequestException:
        return JSONResponse({"detail": "assistant_backend_unavailable"}, status_code=503)
    return _proxy_response(upstream)


@app.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse({
        "name": "Bot-tazzi — Peppone", "short_name": "Bot-tazzi", "description": "Bot-tazzi, assistente locale con Peppone come volto",
        "start_url": "./", "scope": "./", "display": "standalone",
        "background_color": "#171512", "theme_color": "#171512",
        "icons": [{"src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any maskable"}],
    }, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> PlainTextResponse:
    return PlainTextResponse(
        "self.addEventListener('install',e=>self.skipWaiting());self.addEventListener('activate',e=>e.waitUntil(self.clients.claim()));self.addEventListener('fetch',()=>{});",
        media_type="application/javascript", headers={"cache-control": "no-store"}
    )


@app.get("/icon.svg")
def icon() -> Response:
    svg = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 512 512'><rect width='512' height='512' rx='118' fill='#171512'/><rect x='66' y='66' width='380' height='380' rx='110' fill='#d9a06f'/><path d='M138 190Q256 82 374 190L354 230H158Z' fill='#472b22'/><circle cx='256' cy='285' r='112' fill='#d8a06f'/><path d='M158 246Q256 188 354 246' fill='none' stroke='#472b22' stroke-width='22'/><circle cx='215' cy='276' r='13' fill='#241812'/><circle cx='297' cy='276' r='13' fill='#241812'/><path d='M256 298l-16 34h31' fill='none' stroke='#8c5a37' stroke-width='10'/><path d='M252 342c-31-35-68-20-82 1 27 13 54 16 82 5 28 11 56 8 83-5-14-21-52-36-83-1z' fill='#2a1a15'/><path d='M203 386q53 34 106 0' fill='none' stroke='#7d3f2f' stroke-width='10'/></svg>"""
    return Response(svg, media_type="image/svg+xml", headers={"cache-control": "public, max-age=86400"})


__all__ = ["app"]
