from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
import re
import time
from typing import Annotated, Any, Callable, Literal
from urllib.parse import unquote_plus
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from openshell_backend.accounting_review_api import router as accounting_review_router
from openshell_backend.chat_api import (
    ChatHistoryMessage,
    ChatRequest,
    _provider_status,
    build_chat_messages,
    get_chat_provider,
)
from ralfloop_agent.integration.bottazzi_motor_judge import BotTazziMotorJudge
from ralfloop_agent.integration.execution_provenance import (
    empty_execution_provenance,
    guard_execution_claims,
    metadata_with_provenance,
)
from ralfloop_agent.providers.chat import (
    ChatProvider,
    ChatProviderError,
    OpenAICompatibleChatProvider,
)
from ralfloop_agent.unified_assistant.runtime import (
    run_unified_telegram,
    unified_route_probe,
)
from ralfloop_agent.call_recordings import CallRecordingStore
from ralfloop_agent.unified_assistant.task_queue import (
    BotTazziTaskQueue,
    TaskCategory,
    TaskState,
)
router = APIRouter(prefix="/assistant/v1", tags=["assistant-v1"])
router.include_router(accounting_review_router)
ASSISTANT_UI_PATH = Path(__file__).with_name("bottazzi_ui.html")

DEEP_HINT_RE = re.compile(
    r"\b(?:analizza|approfondisci|architettura|debug|benchmark|confronta|"
    r"strategia|progetta|refactor|dimostra|verifica\s+in\s+profondit[aà])\b",
    re.I,
)
GENERAL_KNOWLEDGE_RE = re.compile(
    r"\b(?:cos['’]?[eè]|che\s+cos['’]?[eè]|cosa\s+(?:significa|vuol\s+dire)|"
    r"che\s+significa|spiegami|spiega|definisci|perch[eéè]|come\s+funziona|"
    r"differenza\s+tra)\b",
    re.I,
)
ACRONYM_RE = re.compile(r"(?<!\w)[A-Z]{2,8}(?!\w)")


class AssistantV1Request(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=32_000)
    history: list[ChatHistoryMessage] = Field(default_factory=list, max_length=48)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)
    mode: Literal["auto", "fast", "deep"] = "auto"
    allow_tools: bool = True
    model: str | None = Field(default=None, min_length=1, max_length=256)
    context: dict[str, Any] = Field(default_factory=dict)


class AssistantV1Response(BaseModel):
    ok: bool = True
    response: str
    route: Literal["unified", "local_chat", "deep_chat"]
    provider: str
    model: str
    session_id: str
    duration_ms: int
    approval_required: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class AssistantTaskCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    description: str = Field(default="", max_length=6000)
    category_hint: TaskCategory | None = None
    deadline_epoch: int | None = Field(default=None, ge=0)
    depends_on: list[str] = Field(default_factory=list, max_length=32)


class AssistantTaskPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rank: int = Field(ge=1)


class AssistantTaskStateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: TaskState
    blocked_reason: str | None = Field(default=None, max_length=1000)


from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags


@lru_cache(maxsize=1)
def get_assistant_flags() -> AssistantFeatureFlags:
    configured = AssistantFeatureFlags.from_env()
    return configured.model_copy(update={"unified_assistant": True})


def get_unified_route_probe() -> Callable[..., dict[str, Any] | None]:
    return unified_route_probe


def get_unified_runner() -> Callable[..., dict[str, Any]]:
    return run_unified_telegram


@lru_cache(maxsize=1)
def get_call_recording_store() -> CallRecordingStore:
    return CallRecordingStore.from_env()

@lru_cache(maxsize=1)
def get_motor_client() -> BotTazziMotorJudge:
    return BotTazziMotorJudge()


@lru_cache(maxsize=1)
def get_task_queue() -> BotTazziTaskQueue:
    return BotTazziTaskQueue.from_env()


_DEFAULT_INFERENCE_CONFIG = (
    Path(__file__).resolve().parents[1]
    / "config" / "ralf" / "inference_runtime.no_ollama.json"
)


@lru_cache(maxsize=4)
def _inference_runtime_config(path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@lru_cache(maxsize=8)
def _cached_fast_lane_provider(
    base_url: str,
    model: str,
    max_tokens: int,
) -> ChatProvider:
    return OpenAICompatibleChatProvider(
        base_url=base_url,
        model=model,
        provider_name="assistant_fast_openai_compat",
        request_options={"temperature": 0, "max_tokens": max_tokens},
    )


def _fast_lane_settings() -> tuple[str | None, str | None, int]:
    config_path = os.getenv(
        "BOTTAZZI_ASSISTANT_INFERENCE_CONFIG", str(_DEFAULT_INFERENCE_CONFIG)
    ).strip()
    payload = _inference_runtime_config(config_path) if config_path else {}
    runtimes = payload.get("runtimes") if isinstance(payload.get("runtimes"), dict) else {}
    runtime_name = str(payload.get("default_runtime") or "")
    runtime = runtimes.get(runtime_name) if isinstance(runtimes, dict) else None
    if not isinstance(runtime, dict) or runtime.get("type") != "openai_compat":
        runtime = {}
    models = runtime.get("models") if isinstance(runtime.get("models"), dict) else {}
    configured_url = str(runtime.get("base_url") or "").strip().rstrip("/")
    configured_model = str(models.get("chat") or models.get("default") or "").strip()
    base_url = os.getenv("BOTTAZZI_ASSISTANT_FAST_BASE_URL", "").strip().rstrip("/") or configured_url
    model = _configured_model("BOTTAZZI_ASSISTANT_FAST_MODEL") or configured_model or None
    try:
        max_tokens = int(os.getenv("BOTTAZZI_ASSISTANT_FAST_MAX_TOKENS", "192"))
    except ValueError:
        max_tokens = 192
    return base_url or None, model, max_tokens if max_tokens > 0 else 192


def get_fast_lane_provider() -> ChatProvider | None:
    base_url, model, max_tokens = _fast_lane_settings()
    if not base_url or not model:
        return None
    return _cached_fast_lane_provider(base_url, model, max_tokens)


def _session_id(request: AssistantV1Request) -> str:
    return request.session_id or str(uuid4())


def _runtime_context(request: AssistantV1Request, session_id: str) -> dict[str, Any]:
    context = dict(request.context)
    context["source"] = "ralf_terminal"
    context["session_id"] = session_id
    context["assistant_surface"] = "assistant_v1"
    terminal_client = dict(context.get("terminal_client") or {})
    terminal_client.setdefault("session_id", session_id)
    context["terminal_client"] = terminal_client
    return context


def _configured_model(name: str) -> str | None:
    configured = os.getenv(name, "").strip()
    return configured or None


def _fast_model(request: AssistantV1Request) -> str | None:
    return request.model or _fast_lane_settings()[1]


def _general_model(request: AssistantV1Request) -> str | None:
    # General conversational wording stays on the small local lane.
    return request.model or _fast_lane_settings()[1]


def _deep_model(request: AssistantV1Request) -> str | None:
    return request.model or _configured_model("BOTTAZZI_ASSISTANT_DEEP_MODEL")


def _deep_requested(request: AssistantV1Request) -> bool:
    if request.mode == "deep":
        return True
    if request.mode == "fast":
        return False
    message = request.message
    hints = len(DEEP_HINT_RE.findall(message))
    return len(message) >= 1200 or hints >= 2


def _general_requested(request: AssistantV1Request) -> bool:
    if request.mode != "auto":
        return False
    return bool(
        GENERAL_KNOWLEDGE_RE.search(request.message)
        or ACRONYM_RE.search(request.message)
    )


def _model_lane(
    request: AssistantV1Request,
) -> tuple[Literal["fast", "general", "deep"], str | None, str]:
    if _deep_requested(request):
        return "deep", _deep_model(request), "deep_request"
    if _general_requested(request):
        return "general", _general_model(request), "general_knowledge_or_ambiguity"
    return "fast", _fast_model(request), "small_first_default"


def _chat_request(request: AssistantV1Request, *, session_id: str) -> ChatRequest:
    return ChatRequest(
        message=request.message,
        history=request.history,
        session_id=session_id,
        model=request.model,
        stream=False,
    )


def _unified_route(
    request: AssistantV1Request,
    *,
    session_id: str,
    route_probe: Callable[..., dict[str, Any] | None],
    flags: AssistantFeatureFlags,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    context = _runtime_context(request, session_id)
    if not request.allow_tools:
        return None, context
    return route_probe(
        request.message,
        context,
        flags_override=flags,
    ), context


@router.get("", response_class=HTMLResponse, include_in_schema=False)
@router.get("/ui", response_class=HTMLResponse, include_in_schema=False)
def assistant_v1_ui() -> HTMLResponse:
    return HTMLResponse(ASSISTANT_UI_PATH.read_text(encoding="utf-8"))


@router.post("/chat", response_model=AssistantV1Response)
def assistant_v1_chat(
    request: AssistantV1Request,
    provider: Annotated[ChatProvider | ChatProviderError, Depends(get_chat_provider)],
    fast_provider: Annotated[ChatProvider | None, Depends(get_fast_lane_provider)],
    flags: Annotated[AssistantFeatureFlags, Depends(get_assistant_flags)],
    route_probe: Annotated[Callable[..., dict[str, Any] | None], Depends(get_unified_route_probe)],
    unified_runner: Annotated[Callable[..., dict[str, Any]], Depends(get_unified_runner)],
    motor: Annotated[BotTazziMotorJudge, Depends(get_motor_client)],
) -> AssistantV1Response:
    started = time.monotonic()
    session_id = _session_id(request)
    route, context = _unified_route(
        request,
        session_id=session_id,
        route_probe=route_probe,
        flags=flags,
    )
    if route is not None:
        try:
            result = unified_runner(
                request.message,
                context,
                flags_override=flags,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail=f"unified_runtime_unavailable:{type(exc).__name__}",
            ) from exc
        metadata = dict(result.get("metadata") or {})
        metadata.update({
            "assistant_version": 1,
            "local_only": True,
            "route_probe": route,
            "route_source": "unified_assistant",
        })
        return AssistantV1Response(
            ok=bool(result.get("ok", True)),
            response=str(result.get("final_answer") or result.get("response") or ""),
            route="unified",
            provider="unified_assistant",
            model="deterministic+mcp",
            session_id=session_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            approval_required=bool(result.get("approval_required")),
            metadata=metadata,
        )
    model_lane, selected_model, routing_reason = _model_lane(request)
    if model_lane == "deep":
        chat_request = _chat_request(request, session_id=session_id)
        try:
            response_text = motor.complete(
                build_chat_messages(chat_request),
                max_tokens=int(os.getenv("BOTTAZZI_ASSISTANT_MOTOR_MAX_TOKENS", "256")),
                task_id=session_id,
                mode="assistant_deep",
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="bottazzi_motor_unavailable") from exc
        provenance = empty_execution_provenance(
            provider="bottazzi_motor",
            endpoint=motor.config.base_url,
            model_id=motor.config.model,
        )
        response_text, claim_blocked = guard_execution_claims(response_text, provenance)
        return AssistantV1Response(
            response=response_text,
            route="deep_chat",
            provider="bottazzi_motor",
            model=motor.config.model,
            session_id=session_id,
            duration_ms=int((time.monotonic() - started) * 1000),
            approval_required=False,
            metadata=metadata_with_provenance({
                "assistant_version": 1,
                "local_only": True,
                "route_source": "bottazzi_motor",
                "reasoning_mode": "deep",
                "model_lane": "deep",
                "routing_reason": routing_reason,
                "motor_lifecycle": "on_demand",
                "glm_enabled": False,
                "tools_allowed": False,
            }, provenance, execution_claim_blocked=claim_blocked),
        )
    active_provider: ChatProvider | ChatProviderError = provider
    if model_lane == "fast" and request.model is None and fast_provider is not None:
        active_provider = fast_provider
    if isinstance(active_provider, ChatProviderError):
        raise HTTPException(
            status_code=_provider_status(active_provider),
            detail=active_provider.code,
        )

    chat_request = _chat_request(request, session_id=session_id)
    try:
        result = active_provider.chat(
            build_chat_messages(chat_request),
            model=selected_model,
        )
    except ChatProviderError as exc:
        raise HTTPException(
            status_code=_provider_status(exc),
            detail=exc.code,
        ) from exc

    provenance = empty_execution_provenance(
        provider=result.provider,
        endpoint=str(result.metadata.get("endpoint") or "") or None,
        model_id=result.model,
    )
    response_text, claim_blocked = guard_execution_claims(
        result.text,
        provenance,
    )
    metadata = metadata_with_provenance(
        {
            **result.metadata,
            "assistant_version": 1,
            "local_only": True,
            "route_source": "local_chat",
            "reasoning_mode": model_lane,
            "model_lane": model_lane,
            "routing_reason": routing_reason,
            "fast_model_configured": bool(_fast_lane_settings()[1]),
            "fast_provider_configured": bool(_fast_lane_settings()[0]),
            "lane_provider": result.provider,
            "general_model_configured": bool(
                _configured_model("BOTTAZZI_ASSISTANT_GENERAL_MODEL")
                or _configured_model("BOTTAZZI_ASSISTANT_DEEP_MODEL")
            ),
            "deep_model_configured": bool(
                _configured_model("BOTTAZZI_ASSISTANT_DEEP_MODEL")
            ),
            "tools_allowed": request.allow_tools,
        },
        provenance,
        execution_claim_blocked=claim_blocked,
    )
    return AssistantV1Response(
        response=response_text,
        route="deep_chat" if model_lane == "deep" else "local_chat",
        provider=result.provider,
        model=result.model,
        session_id=session_id,
        duration_ms=int((time.monotonic() - started) * 1000),
        approval_required=False,
        metadata=metadata,
    )


@router.get("/call-recordings")
def assistant_v1_call_recordings(
    store: Annotated[CallRecordingStore, Depends(get_call_recording_store)],
    limit: int = 50,
) -> dict[str, Any]:
    return {"ok": True, "recordings": store.list(limit=min(max(limit, 1), 200))}


@router.post("/call-recordings", status_code=201)
async def assistant_v1_call_recording_ingest(
    request: Request,
    store: Annotated[CallRecordingStore, Depends(get_call_recording_store)],
) -> dict[str, Any]:
    raw_length = request.headers.get("content-length", "").strip()
    if raw_length.isdigit() and int(raw_length) > store.max_bytes:
        raise HTTPException(status_code=413, detail="recording_too_large")
    content = await request.body()
    filename = unquote_plus(request.headers.get("x-bottazzi-filename", "call-recording"))
    extra = {
        "transport": request.headers.get("x-bottazzi-transport", "android_share"),
        "caller": request.headers.get("x-bottazzi-caller", ""),
        "direction": request.headers.get("x-bottazzi-direction", ""),
        "call_started_at": request.headers.get("x-bottazzi-call-started-at", ""),
    }
    try:
        recording = store.ingest(
            content,
            filename=filename,
            content_type=request.headers.get("content-type", "application/octet-stream"),
            extra=extra,
        )
    except ValueError as exc:
        detail = str(exc)
        code = 413 if detail == "recording_too_large" else 400
        raise HTTPException(status_code=code, detail=detail) from exc
    return {"ok": True, "recording": recording}


@router.get("/tasks")
def assistant_v1_tasks(
    queue: Annotated[BotTazziTaskQueue, Depends(get_task_queue)],
) -> dict[str, Any]:
    return queue.snapshot()


@router.post("/tasks")
def assistant_v1_task_create(
    request: AssistantTaskCreateRequest,
    queue: Annotated[BotTazziTaskQueue, Depends(get_task_queue)],
) -> dict[str, Any]:
    try:
        task = queue.create_task(
            request.title,
            description=request.description,
            category_hint=request.category_hint,
            deadline_epoch=request.deadline_epoch,
            depends_on=tuple(request.depends_on),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.get("/tasks/next")
def assistant_v1_task_next(
    queue: Annotated[BotTazziTaskQueue, Depends(get_task_queue)],
) -> dict[str, Any]:
    entry = queue.next_runnable()
    if entry is None:
        return {"task": None, "runnable": False}
    return entry.model_dump(mode="json")


@router.post("/tasks/{task_id}/pin")
def assistant_v1_task_pin(
    task_id: str,
    request: AssistantTaskPinRequest,
    queue: Annotated[BotTazziTaskQueue, Depends(get_task_queue)],
) -> dict[str, Any]:
    try:
        queue.pin(task_id, request.rank)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return queue.snapshot()


@router.post("/tasks/{task_id}/unpin")
def assistant_v1_task_unpin(
    task_id: str,
    queue: Annotated[BotTazziTaskQueue, Depends(get_task_queue)],
) -> dict[str, Any]:
    try:
        task = queue.unpin(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.post("/tasks/{task_id}/state")
def assistant_v1_task_state(
    task_id: str,
    request: AssistantTaskStateRequest,
    queue: Annotated[BotTazziTaskQueue, Depends(get_task_queue)],
) -> dict[str, Any]:
    try:
        task = queue.set_state(
            task_id,
            request.state,
            blocked_reason=request.blocked_reason,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="task_not_found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "task": task.model_dump(mode="json")}


@router.get("/status")
def assistant_v1_status() -> dict[str, Any]:
    fast_base_url, fast_model, _ = _fast_lane_settings()
    return {
        "ok": True,
        "assistant_version": 1,
        "assistant_name": "Bot-tazzi",
        "local_only": True,
        "cloud_llm_required": False,
        "ui_path": "/assistant/v1",
        "routes": ["unified", "local_chat", "deep_chat", "task_queue"],
        "model_policy": "small_first",
        "task_queue_classifier": "JEV",
        "task_queue_priority_domains": ["money", "love", "family"],
        "task_queue_human_override": "pinned_slot_authoritative",
        "fast_model_configured": bool(fast_base_url and fast_model),
        "general_model_configured": bool(_fast_lane_settings()[1]),
        "deep_model_configured": True,
        "heavy_provider": "bottazzi_motor",
        "heavy_model": "deepseek-v4-flash",
        "heavy_lifecycle": "on_demand",
        "retore_model": "qwen2.5-3b",
        "glm_enabled": False,
        "tool_runtime": "unified_assistant",
        "write_policy": "existing_approval_gates",
    }


__all__ = [
    "AssistantV1Request",
    "AssistantV1Response",
    "assistant_v1_chat",
    "assistant_v1_status",
    "get_assistant_flags",
    "get_unified_route_probe",
    "get_unified_runner",
    "get_motor_client",
    "router",
]
