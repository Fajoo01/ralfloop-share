"""Small same-origin student web application. No generic dispatch or file endpoints."""
from __future__ import annotations

from collections import defaultdict, deque
import json
import logging
import os
import re
from pathlib import Path
import threading
import time
import uuid
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..pedagogy import LearnerProfile

from .application import LearningApplication
from .client import TeacherClient
from .feedback_voice import FeedbackVoiceRegistry
from .fish_tts import FishTTSCache
from .state import State

STATIC = Path(__file__).with_name("static")
log = logging.getLogger("teacher.web")


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Login(Input):
    membership_card_id: str = Field(min_length=1, max_length=256)
    credential: str = Field(min_length=1, max_length=128)


class Generate(Input):
    topic: str = Field(min_length=1, max_length=80)
    activity_type: str | None = Field(default=None, max_length=40)


class Answer(Input):
    answer: str | list[str]
    request_key: str = Field(min_length=8, max_length=100)


class Help(Input):
    mode: Literal["hint", "different", "explain"]
    question: str = Field(default="", max_length=2000)


class Simulation(Input):
    prediction: str | None = Field(default=None, min_length=1, max_length=500)
    variables: dict[str, float | int] | None = None


class Material(Input):
    title: str = Field(min_length=1, max_length=150)
    text: str = Field(min_length=1, max_length=10000)
    rights: Literal["own", "authorized", "public_domain", "compatible_license"]
    kind: Literal["book", "notes", "document"] = "notes"
    chapter: str = Field(default="", max_length=150)
    pages: str = Field(default="", max_length=50)


class MaterialAction(Input):
    action: Literal["summarize", "explain", "exercise", "quiz", "audio"]


class Plan(Input):
    minutes: int = Field(ge=5, le=120)


class AudioPosition(Input):
    chapter: int = Field(ge=0, le=100)
    position: float = Field(ge=0, le=86400, allow_inf_nan=False)


class AudioPrepare(Input):
    chapter: int = Field(ge=0, le=100)


def create_app(state=None, teacher=None, *, origin="http://127.0.0.1:19139", secure_cookie=False, fish_tts=None):
    state = state or State(os.environ.get("TEACHER_WEB_DB", "/var/lib/ralfloop-teacher-web/student.sqlite3"))
    learning = LearningApplication(state, teacher or TeacherClient())
    fish = fish_tts if fish_tts is not None else FishTTSCache.from_env()
    voices = FeedbackVoiceRegistry(fish)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.learning = learning
    app.state.fish_tts = fish
    app.state.feedback_voices = voices
    locks = [threading.Lock() for _ in range(64)]
    rate_lock, rates = threading.Lock(), defaultdict(deque)
    health_lock, health_cache = threading.Lock(), {"checked": 0.0, "ok": False}

    @app.middleware("http")
    async def boundary(request, call_next):
        started, request_id = time.monotonic(), uuid.uuid4().hex
        request.state.request_id = request_id
        host = origin.split("://", 1)[-1]
        if request.headers.get("host") != host:
            return JSONResponse({"error": "Richiesta non consentita."}, status_code=403)
        if request.method == "POST":
            if request.headers.get("origin") != origin or request.headers.get("x-teacher-request") != "1":
                return JSONResponse({"error": "Riapri la pagina e riprova."}, status_code=403)
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"error": "Formato non valido."}, status_code=415)
            # Check actual streamed bytes, including requests without Content-Length.
            payload = bytearray()
            async for chunk in request.stream():
                payload.extend(chunk)
                if len(payload) > 24000:
                    return JSONResponse({"error": "Il contenuto è troppo lungo."}, status_code=413)
            request._body = bytes(payload)
            remote = request.client.host if request.client else "local"
            with rate_lock:
                now = time.monotonic()
                for address in list(rates):
                    while rates[address] and rates[address][0] < now - 60:
                        rates[address].popleft()
                    if not rates[address]: del rates[address]
                bucket = rates[remote]
                if len(bucket) >= 60:
                    return JSONResponse({"error": "Attendi un momento e riprova."}, status_code=429)
                bucket.append(now)
        response = await call_next(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                                 "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
                                 "X-Request-ID": request_id})
        # Never log paths (resource identifiers), raw inputs, cards or model responses.
        log.info("request=%s student=%s method=%s status=%s latency_ms=%d", request_id, getattr(request.state, "student", "anonymous"), request.method, response.status_code, (time.monotonic() - started) * 1000)
        return response

    def student(request: Request):
        try:
            result = state.authenticate(request.cookies.get("teacher_session", ""))
        except PermissionError:
            raise HTTPException(401, "Accedi con la tua tessera.")
        request.state.student = result["id"][:12]
        result["learner_profile"] = state.learner_profile(result)
        return result

    def locked(profile, function, *args):
        lock = locks[int(profile["id"][:4], 16) % len(locks)]
        if not lock.acquire(timeout=1):
            raise HTTPException(409, "Sto già preparando la tua attività. Attendi.")
        try:
            return function(profile, *args)
        finally:
            lock.release()

    def owned_audio_track(profile, asset_id: str, chapter: int):
        asset = state.owned("audio_assets", profile["id"], asset_id)
        tracks = json.loads(asset["tracks"])
        if chapter >= len(tracks):
            raise ValueError("chapter_unavailable")
        text = tracks[chapter].get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("chapter_unavailable")
        return asset, text

    @app.exception_handler(Exception)
    async def error(request, exc):
        log.warning("request=%s error_class=%s", getattr(request.state, "request_id", "unknown"), type(exc).__name__)
        return JSONResponse({"error": "Il tutor non è disponibile in questo momento. Riprova: i tuoi progressi sono al sicuro."}, status_code=503)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"error": "Attività non disponibile o dati non validi. Prova una nuova attività."}, status_code=400)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request, exc):
        # FastAPI's default validation response echoes input, possibly credentials.
        return JSONResponse({"error": "Controlla i campi e riprova."}, status_code=422)

    @app.exception_handler(LookupError)
    async def missing(request, exc):
        return JSONResponse({"error": "Contenuto non disponibile."}, status_code=404)

    @app.get("/health")
    def health():
        try:
            with state.connect() as conn:
                conn.execute("SELECT version FROM schema_version").fetchone()
            with health_lock:
                if time.monotonic() - health_cache["checked"] > 15:
                    health_cache["ok"] = learning.teacher.health()
                    health_cache["checked"] = time.monotonic()
                ok = health_cache["ok"]
            return JSONResponse({"status": "ok" if ok else "degraded"}, status_code=200 if ok else 503)
        except Exception as exc:
            log.warning("health error_class=%s", type(exc).__name__)
            return JSONResponse({"status": "unavailable"}, status_code=503)

    @app.post("/api/login")
    def login(data: Login, request: Request):
        try:
            token = state.login(data.membership_card_id, data.credential, request.client.host if request.client else "local")
        except PermissionError:
            raise HTTPException(401, "Tessera o credenziale non valida. Se hai riprovato molte volte, attendi cinque minuti.")
        response = JSONResponse({"ok": True})
        response.set_cookie("teacher_session", token, httponly=True, secure=secure_cookie, samesite="strict", max_age=28800, path="/")
        return response

    @app.post("/api/logout")
    def logout(request: Request, profile=Depends(student)):
        state.logout(request.cookies.get("teacher_session", ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie("teacher_session")
        return response

    @app.get("/api/home")
    def home(profile=Depends(student)): return learning.home(profile)

    @app.get("/api/progress")
    def progress(profile=Depends(student)): return state.progress(profile["id"])

    @app.get("/api/learner-profile")
    def learner_profile(profile=Depends(student)):
        return profile["learner_profile"]

    @app.post("/api/learner-profile")
    def learner_profile_update(data: LearnerProfile, profile=Depends(student)):
        return state.set_learner_profile(profile["id"], data.model_dump(mode="json"))

    @app.post("/api/activities")
    def generate(data: Generate, profile=Depends(student)):
        result = locked(profile, learning.generate, data.topic, data.activity_type)
        try:
            warmup = getattr(fish, "warmup", None)
            if callable(warmup):
                warmup()
        except Exception as exc:
            log.info("fish_tts_warmup_skipped error_class=%s", type(exc).__name__)
        return result

    @app.get("/api/activities/{activity_id}")
    def activity(activity_id: str, profile=Depends(student)):
        return learning.public_activity(state.owned("activities", profile["id"], activity_id))

    @app.post("/api/activities/{activity_id}/answer")
    def answer(activity_id: str, data: Answer, profile=Depends(student)):
        if (isinstance(data.answer, str) and len(data.answer) > 4000) or (isinstance(data.answer, list) and (len(data.answer) > 12 or any(len(x) > 2000 for x in data.answer))):
            raise ValueError("answer_too_large")
        result = locked(profile, learning.answer, activity_id, data.answer, data.request_key)
        return voices.attach(profile["id"], result)

    @app.post("/api/activities/{activity_id}/help")
    def help_activity(activity_id: str, data: Help, profile=Depends(student)):
        result = locked(profile, learning.help, activity_id, data.mode, data.question)
        return voices.attach(profile["id"], result)

    @app.post("/api/activities/{activity_id}/help/stream")
    def help_activity_stream(activity_id: str, data: Help, profile=Depends(student)):
        lock = locks[int(profile["id"][:4], 16) % len(locks)]
        if not lock.acquire(timeout=1):
            raise HTTPException(409, "Sto già preparando la tua attività. Attendi.")

        def ndjson():
            sentence_buffer = ""

            def voice_event(sentence: str):
                prepared = voices.attach(profile["id"], {"feedback": sentence})
                voice = prepared.get("voice") if isinstance(prepared, dict) else None
                if not isinstance(voice, dict):
                    return {"type": "voice", "text": sentence, "status": "browser_fallback"}
                status = voices.prepare(profile["id"], voice["id"])
                return {
                    "type": "voice",
                    "text": sentence,
                    "status": status.get("status", "pending"),
                    "url": status.get("url"),
                }

            try:
                for event in learning.help_stream(profile, activity_id, data.mode, data.question):
                    if event.get("type") == "delta":
                        text = event.get("text")
                        if isinstance(text, str) and text:
                            sentence_buffer += text
                            yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                            while True:
                                match = re.search(r"(?<=[.!?])(?:\s+|$)", sentence_buffer)
                                if not match:
                                    break
                                sentence = sentence_buffer[:match.end()].strip()
                                sentence_buffer = sentence_buffer[match.end():]
                                if sentence:
                                    yield json.dumps(voice_event(sentence), ensure_ascii=False, separators=(",", ":")) + "\n"
                        continue
                    if event.get("type") == "done" and isinstance(event.get("result"), dict):
                        if sentence_buffer.strip():
                            yield json.dumps(voice_event(sentence_buffer.strip()), ensure_ascii=False, separators=(",", ":")) + "\n"
                            sentence_buffer = ""
                        event = {"type": "done", "result": voices.attach(profile["id"], event["result"])}
                    yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            finally:
                lock.release()

        return StreamingResponse(
            ndjson(),
            media_type="application/x-ndjson",
            headers={"X-Accel-Buffering": "no"},
        )

    @app.post("/api/activities/{activity_id}/simulation")
    def simulation(activity_id: str, data: Simulation, profile=Depends(student)):
        return locked(profile, learning.simulate, activity_id, data.prediction, data.variables)

    @app.get("/api/materials")
    def materials(profile=Depends(student)): return learning.materials(profile)

    @app.post("/api/materials")
    def add_material(data: Material, profile=Depends(student)):
        return locked(profile, lambda p: learning.add_material(p, **data.model_dump()))

    @app.post("/api/materials/{material_id}")
    def material_action(material_id: str, data: MaterialAction, profile=Depends(student)):
        result = locked(profile, learning.material_action, material_id, data.action)
        return voices.attach(profile["id"], result)

    @app.post("/api/study-plan")
    def plan(data: Plan, profile=Depends(student)):
        return locked(profile, learning.plan, data.minutes)

    @app.get("/api/audio")
    def audio(profile=Depends(student)):
        with state.connect() as conn:
            rows = conn.execute("SELECT a.id,a.provider,a.tracks,a.chapter,a.position,m.title FROM audio_assets a JOIN materials m ON m.id=a.material WHERE a.student=? ORDER BY a.created DESC LIMIT 30", (profile["id"],)).fetchall()
        return [{**dict(row), "tracks": json.loads(row["tracks"])} for row in rows]

    @app.post("/api/feedback-audio/{voice_id}/prepare")
    def feedback_audio_prepare(voice_id: str, profile=Depends(student)):
        return voices.prepare(profile["id"], voice_id)

    @app.get("/api/feedback-audio/{voice_id}/file")
    def feedback_audio_file(voice_id: str, profile=Depends(student)):
        path = voices.ready_path(profile["id"], voice_id)
        if path is None:
            raise HTTPException(404, "Audio non ancora pronto.")
        return FileResponse(path, media_type="audio/wav")

    @app.post("/api/audio/{asset_id}/position")
    def audio_position(asset_id: str, data: AudioPosition, profile=Depends(student)):
        asset = state.owned("audio_assets", profile["id"], asset_id)
        if data.chapter >= len(json.loads(asset["tracks"])): raise ValueError("chapter_unavailable")
        with state.connect() as conn:
            conn.execute("UPDATE audio_assets SET chapter=?,position=? WHERE id=? AND student=?", (data.chapter, data.position, asset_id, profile["id"]))
        return {"ok": True}

    @app.post("/api/audio/{asset_id}/prepare")
    def audio_prepare(asset_id: str, data: AudioPrepare, profile=Depends(student)):
        _, text = owned_audio_track(profile, asset_id, data.chapter)
        result = dict(fish.prepare(text))
        if result.get("status") == "ready":
            result["url"] = f"/api/audio/{asset_id}/file/{data.chapter}"
        return result

    @app.get("/api/audio/{asset_id}/file/{chapter}")
    def audio_file(asset_id: str, chapter: int, profile=Depends(student)):
        if chapter < 0 or chapter > 100:
            raise ValueError("chapter_unavailable")
        _, text = owned_audio_track(profile, asset_id, chapter)
        path = fish.ready_path(text)
        if path is None:
            raise HTTPException(404, "Audio non ancora pronto.")
        return FileResponse(path, media_type="audio/wav")

    @app.get("/assets/{name}")
    def asset(name: str):
        if name not in ("app.js", "style.css", "bot-tazzi.jpeg", "avatar_controller.js", "tutor-theme.css", "tutor-avatar.svg"):
            raise HTTPException(404)
        return FileResponse(STATIC / name)

    for route in ("/", "/login", "/home", "/study", "/activity", "/quiz", "/simulations", "/books", "/progress", "/badges", "/audio", "/profile"):
        app.add_api_route(route, lambda: FileResponse(STATIC / "index.html"), methods=["GET"])
    return app
