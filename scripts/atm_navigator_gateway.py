from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

UPSTREAM = os.environ.get("ATM_NAV_UPSTREAM", "http://127.0.0.1:19090").rstrip("/")
app = FastAPI(title="Tiremm ATM Navigator Gateway", docs_url=None, redoc_url=None, openapi_url=None)

ROUTES = {
    ("GET", "/destinations"): ("GET", "/atm-telegram/api/destinations"),
    ("POST", "/navigator/start"): ("POST", "/atm-telegram/api/navigator/start"),
    ("POST", "/navigator/update"): ("POST", "/atm-telegram/api/navigator/update"),
    ("POST", "/navigator/stop"): ("POST", "/atm-telegram/api/navigator/stop"),
}

def _proxy(method: str, path: str, body: bytes | None = None) -> JSONResponse:
    req = urllib.request.Request(
        UPSTREAM + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json", "User-Agent": "Tiremm-ATM-Navigator-Gateway/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=35) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"upstream_unreachable:{exc}") from exc
    try:
        data: Any = json.loads(payload.decode("utf-8"))
    except Exception:
        data = {"raw": payload.decode("utf-8", errors="replace")}
    return JSONResponse(data, status_code=status)

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

@app.get("/destinations")
def destinations() -> JSONResponse:
    return _proxy("GET", "/atm-telegram/api/destinations")

@app.post("/navigator/start")
async def nav_start(request: Request) -> JSONResponse:
    return _proxy("POST", "/atm-telegram/api/navigator/start", await request.body())

@app.post("/navigator/update")
async def nav_update(request: Request) -> JSONResponse:
    return _proxy("POST", "/atm-telegram/api/navigator/update", await request.body())

@app.post("/navigator/stop")
async def nav_stop(request: Request) -> JSONResponse:
    return _proxy("POST", "/atm-telegram/api/navigator/stop", await request.body())

@app.get("/navigator/{session_id}")
def nav_status(session_id: str) -> JSONResponse:
    safe = "".join(ch for ch in session_id if ch.isalnum() or ch in "-_")
    if safe != session_id or not safe:
        raise HTTPException(status_code=400, detail="invalid_session_id")
    return _proxy("GET", f"/atm-telegram/api/navigator/{safe}")
