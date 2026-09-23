from __future__ import annotations

import base64
import hashlib
import hmac
import os
from pathlib import Path
import time
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
import requests

APP_NAME = "Bot-tazzi — App"
UI_PATH = Path(__file__).with_name("bottazzi_ui.html")
BACKEND = os.getenv("BOTTAZZI_APP_BACKEND", "http://127.0.0.1:19090").rstrip("/")
COOKIE = "bottazzi_app_session"
SESSION_TTL = int(os.getenv("BOTTAZZI_APP_SESSION_TTL", "86400"))
PUBLIC_PATHS = {"/login", "/healthz", "/manifest.webmanifest", "/sw.js", "/icon.svg"}

app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)


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
        return RedirectResponse(_url(request, "/login"), status_code=303)
    return JSONResponse({"detail": "app_auth_required"}, status_code=401)


LOGIN_HTML = """<!doctype html><html lang='it'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1,viewport-fit=cover'><meta name='theme-color' content='#171512'><title>Bot-tazzi — Accesso privato</title><style>:root{color-scheme:dark;font-family:Inter,system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;min-height:100dvh;display:grid;place-items:center;background:radial-gradient(circle at 80% 0,#38251f 0,transparent 30%),#171512;color:#f3ead8}.card{width:min(430px,calc(100vw - 32px));background:#211e1a;border:1px solid #3c362e;border-radius:22px;padding:28px;box-shadow:0 22px 60px #0008}.mark{width:72px;height:72px;border-radius:22px;display:grid;place-items:center;background:linear-gradient(145deg,#f0d3aa,#b87d51);overflow:hidden}.mark svg{width:70px;height:70px}h1{margin:18px 0 8px;font-size:30px}p{color:#b6aa96;line-height:1.5}input,button{width:100%;font:inherit;border-radius:12px}input{margin-top:12px;padding:14px;background:#171512;border:1px solid #4a4238;color:#f3ead8;outline:none}button{margin-top:12px;padding:13px;border:0;background:#e34b3d;color:white;font-weight:800;cursor:pointer}.err{min-height:20px;color:#ef9389;font-size:13px;margin-top:10px}</style></head><body><main class='card'><div class='mark'><svg viewBox='0 0 100 100' aria-label='Peppone'><path d='M20 30Q50 8 80 30L76 40H24Z' fill='#472b22'/><circle cx='50' cy='55' r='27' fill='#d8a06f'/><path d='M26 44Q50 30 74 44' fill='none' stroke='#472b22' stroke-width='5'/><circle cx='40' cy='52' r='3' fill='#241812'/><circle cx='60' cy='52' r='3' fill='#241812'/><path d='M50 57l-3 8h6' fill='none' stroke='#8c5a37' stroke-width='2'/><path d='M49 67c-7-8-15-5-18 0 6 2 12 3 18 1 6 2 12 1 18-1-3-5-11-8-18 0z' fill='#2a1a15'/><path d='M37 76q13 8 26 0' fill='none' stroke='#7d3f2f' stroke-width='2'/></svg></div><h1>Bot-tazzi</h1><p>Peppone · assistente locale. Accesso privato alla tua infrastruttura.</p><form id='f'><input id='p' type='password' autocomplete='current-password' autofocus placeholder='Password Bot-tazzi'><button>Entra</button><div class='err' id='e'></div></form></main><script>document.querySelector('#f').addEventListener('submit',async e=>{e.preventDefault();const out=document.querySelector('#e');out.textContent='';const r=await fetch('./login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({password:document.querySelector('#p').value})});const j=await r.json().catch(()=>({}));if(r.ok){location.href=j.next||'./'}else out.textContent='Password errata';});</script></body></html>"""


@app.get("/login", response_class=HTMLResponse)
def login_page() -> HTMLResponse:
    return HTMLResponse(LOGIN_HTML, headers={"cache-control": "no-store"})


@app.post("/login")
async def login(request: Request) -> JSONResponse:
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
    return {"ok": True, "service": "bottazzi-app-gateway"}


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
            payload.update({"app_gateway": True, "app_private": True, "app_modules": ["local_chat", "internet_agent", "tools", "fast", "deep"]})
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


@app.post("/assistant/v1/chat")
def assistant_chat(payload: dict[str, Any]) -> Response:
    outgoing = dict(payload)
    internet_agent = bool(outgoing.pop("app_internet_agent", False))
    if internet_agent:
        original = str(outgoing.get("message") or "").strip()
        outgoing["message"] = (
            "Fai una ricerca approfondita su Internet in sola lettura, con fonti e provenance. "
            "Usa prima fonti primarie o manuali ufficiali, poi GitHub upstream e forum tecnici quando pertinenti. "
            "Rispondi alla domanda originale senza trasformarla in un'altra richiesta. Domanda originale: " + original
        )[:32_000]
        outgoing["allow_tools"] = True
        context = dict(outgoing.get("context") or {})
        context["app_internet_agent"] = True
        outgoing["context"] = context
    try:
        upstream = requests.post(
            f"{BACKEND}/assistant/v1/chat",
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
