from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
import os
import time

import requests

from ralfloop_agent.providers.chat import (
    ChatChunk,
    ChatConnectTimeout,
    ChatInactivityTimeout,
    ChatInvalidResponse,
    ChatProvider,
    ChatProviderError,
    ChatProviderHTTPError,
    ChatProviderSettings,
    ChatProviderUnavailable,
    ChatResult,
    FallbackChatProvider,
    OpenAICompatibleChatProvider,
)
from ralfloop_agent.providers.llama_cpp_server import (
    LlamaCppServerConfig,
    LlamaCppServerError,
    LlamaCppServerManager,
)


def _connect_timeout() -> float:
    try:
        value = float(os.getenv("RALF_CHAT_CONNECT_TIMEOUT", "2"))
    except ValueError:
        return 2.0
    return value if value > 0 else 2.0


def _fallback_allowed(exc: ChatProviderError) -> bool:
    if isinstance(exc, ChatInactivityTimeout):
        return False
    if str(exc) in {
        "agent_gpu_lock_busy",
        "agent_task_active",
        "engine_busy",
        "llama_cpp_gpu_lock_busy",
    }:
        return False
    if isinstance(exc, (ChatProviderUnavailable, ChatConnectTimeout, ChatInvalidResponse)):
        return True
    if isinstance(exc, ChatProviderHTTPError):
        return True
    return False


class LlamaCppChatProvider:
    name = "llama_cpp"

    def __init__(
        self,
        *,
        config: LlamaCppServerConfig | None = None,
        manager: LlamaCppServerManager | None = None,
        session: requests.Session | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or LlamaCppServerConfig.from_env()
        self.default_model = self.config.model
        self.manager = manager or LlamaCppServerManager(self.config, session=session)
        self.monotonic = monotonic
        self.target = OpenAICompatibleChatProvider(
            base_url=self.config.base_url,
            model=self.config.model,
            provider_name=self.name,
            settings=ChatProviderSettings(
                connect_timeout_sec=_connect_timeout(),
                inactivity_timeout_sec=self.config.idle_timeout_sec,
            ),
            session=session,
            request_options={
                "cache_prompt": self.config.cache_prompt,
                "stream_options": {"include_usage": True},
            },
        )

    def _ensure(self) -> dict[str, object]:
        try:
            return self.manager.ensure_available()
        except LlamaCppServerError as exc:
            raise ChatProviderUnavailable(exc.code) from exc

    def _metadata(self, metadata: Mapping[str, object], server: Mapping[str, object]) -> dict[str, object]:
        return {
            **metadata,
            **server,
            "provider": self.name,
            "model": self.config.model,
            "endpoint": self.config.base_url,
            "gpu_layers": self.config.gpu_layers,
            "prompt_cache": self.config.cache_prompt,
        }

    def chat(self, messages: Sequence[Mapping[str, str]], *, model: str | None = None) -> ChatResult:
        server = self._ensure()
        started = self.monotonic()
        result = self.target.chat(messages, model=model)
        metadata = self._metadata(result.metadata, server)
        metadata["wall_ms"] = (self.monotonic() - started) * 1000
        return ChatResult(result.text, result.model, self.name, metadata)

    def stream_chat(self, messages: Sequence[Mapping[str, str]], *, model: str | None = None) -> Iterator[ChatChunk]:
        server = self._ensure()
        started = self.monotonic()
        first_token: float | None = None
        stream = self.target.stream_chat(messages, model=model)
        try:
            for chunk in stream:
                if chunk.text and first_token is None:
                    first_token = self.monotonic()
                if chunk.done:
                    metadata = self._metadata(chunk.metadata, server)
                    metadata["wall_ms"] = (self.monotonic() - started) * 1000
                    if first_token is not None:
                        metadata["ttft_ms"] = (first_token - started) * 1000
                    yield ChatChunk(
                        text=chunk.text,
                        done=True,
                        model=chunk.model,
                        metadata=metadata,
                    )
                else:
                    yield chunk
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()


def build_llama_cpp_chat_provider(
    *,
    fallback: ChatProvider | None,
    session: requests.Session | None = None,
    config: LlamaCppServerConfig | None = None,
    manager: LlamaCppServerManager | None = None,
) -> ChatProvider:
    selected_config = config or LlamaCppServerConfig.from_env()
    primary: ChatProvider = LlamaCppChatProvider(
        config=selected_config,
        manager=manager,
        session=session,
    )
    if selected_config.fallback == "ollama" and fallback is not None:
        return FallbackChatProvider(
            name="llama_cpp",
            primary=primary,
            fallback=fallback,
            should_fallback=_fallback_allowed,
        )
    return primary


__all__ = [
    "LlamaCppChatProvider",
    "build_llama_cpp_chat_provider",
]
