from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import re
import time
from typing import Annotated, Any, Callable, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from openshell_backend.chat_api import (
    ChatHistoryMessage,
    ChatRequest,
    _provider_status,
    build_chat_messages,
    get_chat_provider,
)
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
router = APIRouter(prefix="/assistant/v1", tags=["assistant-v1"])
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
from ralfloop_agent.unified_assistant.contracts import AssistantFeatureFlags


@lru_cache(maxsize=1)
def get_assistant_flags() -> AssistantFeatureFlags:
    configured = AssistantFeatureFlags.from_env()
    return configured.model_copy(update={"unified_assistant": True})


def get_unified_route_probe() -> Callable[..., dict[str, Any] | None]:
    return unified_route_probe


def get_unified_runner() -> Callable[..., dict[str, Any]]:
    return run_unified_telegram


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


def get_fast_lane_provider() -> ChatProvider | None:
    base_url = os.getenv("BOTTAZZI_ASSISTANT_FAST_BASE_URL", "").strip().rstrip("/")
    model = _configured_model("BOTTAZZI_ASSISTANT_FAST_MODEL")
    if not base_url or not model:
        return None
    try:
        max_tokens = int(os.getenv("BOTTAZZI_ASSISTANT_FAST_MAX_TOKENS", "192"))
    except ValueError:
        max_tokens = 192
    if max_tokens <= 0:
        max_tokens = 192
    return _cached_fast_lane_provider(base_url, model, max_tokens)


def _session_id(request: AssistantV1Request) -> str:
    return request.session_id or str(uuid4())


def _runtime_context(request: AssistantV1Request, session_id: str) -> dict[str, Any]:
    context = dict(request.context)
    context["source"] = "ralf_terminal"
    context["session_id"] = session_id
    context["assistant_surface"] = "assistant_v1"
    return context


def _configured_model(name: str) -> str | None:
    configured = os.getenv(name, "").strip()
    return configured or None


def _fast_model(request: AssistantV1Request) -> str | None:
    return request.model or _configured_model("BOTTAZZI_ASSISTANT_FAST_MODEL")


def _general_model(request: AssistantV1Request) -> str | None:
    return (
        request.model
        or _configured_model("BOTTAZZI_ASSISTANT_GENERAL_MODEL")
        or _configured_model("BOTTAZZI_ASSISTANT_DEEP_MODEL")
    )


def _deep_model(request: AssistantV1Request) -> str | None:
    return request.model or _configured_model("BOTTAZZI_ASSISTANT_DEEP_MODEL")


def _deep_requested(request: AssistantV1Request) -> bool:
    if request.mode == "deep":
        return True
    if request.mode == "fast":
        return False
    if not _configured_model("BOTTAZZI_ASSISTANT_DEEP_MODEL"):
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
            "fast_model_configured": bool(
                _configured_model("BOTTAZZI_ASSISTANT_FAST_MODEL")
            ),
            "fast_provider_configured": bool(
                os.getenv("BOTTAZZI_ASSISTANT_FAST_BASE_URL", "").strip()
            ),
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
@router.get("/status")
def assistant_v1_status() -> dict[str, Any]:
    return {
        "ok": True,
        "assistant_version": 1,
        "assistant_name": "Bot-tazzi",
        "local_only": True,
        "cloud_llm_required": False,
        "ui_path": "/assistant/v1",
        "routes": ["unified", "local_chat", "deep_chat"],
        "model_policy": "small_first",
        "fast_model_configured": bool(
            _configured_model("BOTTAZZI_ASSISTANT_FAST_MODEL")
        ),
        "general_model_configured": bool(
            _configured_model("BOTTAZZI_ASSISTANT_GENERAL_MODEL")
            or _configured_model("BOTTAZZI_ASSISTANT_DEEP_MODEL")
        ),
        "deep_model_configured": bool(
            _configured_model("BOTTAZZI_ASSISTANT_DEEP_MODEL")
        ),
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
    "router",
]
