from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
from typing import Any, Callable, Protocol

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNTIME_CONFIG = PROJECT_ROOT / "config" / "ralf" / "inference_runtime.json"
DEFAULT_CONNECT_TIMEOUT_SEC = 2.0
DEFAULT_INACTIVITY_TIMEOUT_SEC = 60.0


class ChatProviderError(RuntimeError):
    code = "provider_error"


class ChatProviderConfigurationError(ChatProviderError):
    code = "provider_configuration_error"


class ChatProviderUnavailable(ChatProviderError):
    code = "provider_unavailable"


class ChatConnectTimeout(ChatProviderError):
    code = "provider_connect_timeout"


class ChatInactivityTimeout(ChatProviderError):
    code = "provider_inactivity_timeout"


class ChatProviderHTTPError(ChatProviderError):
    code = "provider_http_error"

    def __init__(self, status_code: int | None = None) -> None:
        self.status_code = status_code
        suffix = f":{status_code}" if status_code is not None else ""
        super().__init__(f"{self.code}{suffix}")


class ChatInvalidResponse(ChatProviderError):
    code = "invalid_provider_response"


@dataclass(frozen=True)
class ChatProviderSettings:
    connect_timeout_sec: float = DEFAULT_CONNECT_TIMEOUT_SEC
    inactivity_timeout_sec: float = DEFAULT_INACTIVITY_TIMEOUT_SEC

    def __post_init__(self) -> None:
        if self.connect_timeout_sec <= 0:
            raise ValueError("connect_timeout_sec must be positive")
        if self.inactivity_timeout_sec <= 0:
            raise ValueError("inactivity_timeout_sec must be positive")

    @property
    def requests_timeout(self) -> tuple[float, float]:
        return (self.connect_timeout_sec, self.inactivity_timeout_sec)


@dataclass(frozen=True)
class ChatResult:
    text: str
    model: str
    provider: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ChatChunk:
    text: str = ""
    done: bool = False
    model: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class ChatProvider(Protocol):
    name: str
    default_model: str

    def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
    ) -> ChatResult:
        ...

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
    ) -> Iterator[ChatChunk]:
        ...


def _safe_metadata(payload: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in keys:
        if key not in payload:
            continue
        value = payload.get(key)
        if isinstance(value, (str, int, float, bool)) or value is None:
            metadata[key] = value
    return metadata


def _numeric_object(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            result[key] = item
        elif isinstance(item, Mapping):
            nested = _numeric_object(item)
            if nested:
                result[key] = nested
    return result


def _openai_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    usage = _numeric_object(payload.get("usage"))
    timings = _numeric_object(payload.get("timings"))
    if usage:
        metadata["usage"] = usage
        metadata["prompt_tokens"] = usage.get("prompt_tokens")
        metadata["generated_tokens"] = usage.get("completion_tokens")
        details = usage.get("prompt_tokens_details")
        if isinstance(details, Mapping) and isinstance(details.get("cached_tokens"), (int, float)):
            metadata["cache_hit_tokens"] = details["cached_tokens"]
    if timings:
        metadata["timings"] = timings
        metadata["prompt_eval_ms"] = timings.get("prompt_ms")
        metadata["prompt_tokens_per_second"] = timings.get("prompt_per_second")
        metadata["decode_tokens_per_second"] = timings.get("predicted_per_second")
        if metadata.get("cache_hit_tokens") is None:
            metadata["cache_hit_tokens"] = timings.get("cache_n")
    return {key: value for key, value in metadata.items() if value is not None}


def _translate_transport_error(exc: requests.RequestException) -> ChatProviderError:
    if isinstance(exc, requests.ConnectTimeout):
        return ChatConnectTimeout(ChatConnectTimeout.code)
    if isinstance(exc, requests.ReadTimeout):
        return ChatInactivityTimeout(ChatInactivityTimeout.code)
    if isinstance(exc, requests.Timeout):
        return ChatInactivityTimeout(ChatInactivityTimeout.code)
    if "read timed out" in str(exc).lower():
        return ChatInactivityTimeout(ChatInactivityTimeout.code)
    return ChatProviderUnavailable(ChatProviderUnavailable.code)


def _check_response(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        status_code = getattr(response, "status_code", None)
        response.close()
        raise ChatProviderHTTPError(status_code) from exc


def _decode_json_line(raw_line: bytes | str, *, source: str) -> dict[str, Any]:
    try:
        if isinstance(raw_line, bytes):
            raw_line = raw_line.decode("utf-8", errors="strict")
        payload = json.loads(raw_line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatInvalidResponse(f"{source}_invalid_json") from exc
    if not isinstance(payload, dict):
        raise ChatInvalidResponse(f"{source}_event_not_object")
    return payload


class OllamaChatProvider:
    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        settings: ChatProviderSettings | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_model = model
        self.settings = settings or ChatProviderSettings()
        self.session = session or requests.Session()

    def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
    ) -> ChatResult:
        selected_model = _selected_model(model, self.default_model)
        try:
            response = self.session.post(
                f"{self.base_url}/api/chat",
                json={"model": selected_model, "messages": list(messages), "stream": False},
                headers={"Accept": "application/json"},
                timeout=self.settings.requests_timeout,
            )
        except requests.RequestException as exc:
            raise _translate_transport_error(exc) from exc

        try:
            _check_response(response)
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise ChatInvalidResponse("ollama_invalid_json") from exc
        finally:
            response.close()

        if not isinstance(payload, dict):
            raise ChatInvalidResponse("ollama_response_not_object")
        message = payload.get("message")
        text = message.get("content") if isinstance(message, dict) else None
        if not isinstance(text, str):
            raise ChatInvalidResponse("ollama_response_missing_content")
        actual_model = str(payload.get("model") or selected_model)
        metadata = _safe_metadata(
            payload,
            (
                "done_reason",
                "total_duration",
                "load_duration",
                "prompt_eval_count",
                "prompt_eval_duration",
                "eval_count",
                "eval_duration",
            ),
        )
        return ChatResult(text=text, model=actual_model, provider=self.name, metadata=metadata)

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
    ) -> Iterator[ChatChunk]:
        selected_model = _selected_model(model, self.default_model)
        response: requests.Response | None = None
        try:
            try:
                response = self.session.post(
                    f"{self.base_url}/api/chat",
                    json={"model": selected_model, "messages": list(messages), "stream": True},
                    headers={"Accept": "application/x-ndjson"},
                    stream=True,
                    timeout=self.settings.requests_timeout,
                )
                _check_response(response)
                lines = response.iter_lines(chunk_size=1, decode_unicode=True)
                saw_done = False
                for raw_line in lines:
                    if not raw_line:
                        continue
                    payload = _decode_json_line(raw_line, source="ollama_stream")
                    if payload.get("error"):
                        raise ChatProviderError("ollama_provider_error")
                    actual_model = str(payload.get("model") or selected_model)
                    message = payload.get("message")
                    text = message.get("content") if isinstance(message, dict) else ""
                    if text is not None and not isinstance(text, str):
                        raise ChatInvalidResponse("ollama_stream_content_not_text")
                    if text:
                        yield ChatChunk(text=text, model=actual_model)
                    if payload.get("done") is True:
                        metadata = _safe_metadata(
                            payload,
                            (
                                "done_reason",
                                "total_duration",
                                "load_duration",
                                "prompt_eval_count",
                                "prompt_eval_duration",
                                "eval_count",
                                "eval_duration",
                            ),
                        )
                        yield ChatChunk(done=True, model=actual_model, metadata=metadata)
                        saw_done = True
                        break
                if not saw_done:
                    raise ChatInvalidResponse("ollama_stream_missing_done")
            except requests.RequestException as exc:
                raise _translate_transport_error(exc) from exc
        finally:
            if response is not None:
                response.close()


class OpenAICompatibleChatProvider:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        provider_name: str = "openai_compat",
        settings: ChatProviderSettings | None = None,
        session: requests.Session | None = None,
        request_options: Mapping[str, Any] | None = None,
    ) -> None:
        self.name = provider_name
        self.base_url = base_url.rstrip("/")
        self.default_model = model
        self.settings = settings or ChatProviderSettings()
        self.session = session or requests.Session()
        self.request_options = dict(request_options or {})

    def chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
    ) -> ChatResult:
        selected_model = _selected_model(model, self.default_model)
        try:
            response = self.session.post(
                f"{self.base_url}/v1/chat/completions",
                json={**self.request_options, "model": selected_model, "messages": list(messages), "stream": False},
                headers={"Accept": "application/json"},
                timeout=self.settings.requests_timeout,
            )
        except requests.RequestException as exc:
            raise _translate_transport_error(exc) from exc

        try:
            _check_response(response)
            try:
                payload = response.json()
            except (TypeError, ValueError) as exc:
                raise ChatInvalidResponse("openai_compat_invalid_json") from exc
        finally:
            response.close()

        if not isinstance(payload, dict):
            raise ChatInvalidResponse("openai_compat_response_not_object")
        try:
            text = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatInvalidResponse("openai_compat_response_missing_content") from exc
        if not isinstance(text, str):
            raise ChatInvalidResponse("openai_compat_response_content_not_text")
        actual_model = str(payload.get("model") or selected_model)
        metadata = _openai_metadata(payload)
        return ChatResult(text=text, model=actual_model, provider=self.name, metadata=metadata)

    def stream_chat(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        model: str | None = None,
    ) -> Iterator[ChatChunk]:
        selected_model = _selected_model(model, self.default_model)
        response: requests.Response | None = None
        actual_model = selected_model
        finish_reason: str | None = None
        final_metadata: dict[str, Any] = {}
        try:
            try:
                response = self.session.post(
                    f"{self.base_url}/v1/chat/completions",
                    json={**self.request_options, "model": selected_model, "messages": list(messages), "stream": True},
                    headers={"Accept": "text/event-stream"},
                    stream=True,
                    timeout=self.settings.requests_timeout,
                )
                _check_response(response)
                saw_done = False
                for raw_line in response.iter_lines(chunk_size=1, decode_unicode=True):
                    if not raw_line:
                        continue
                    if isinstance(raw_line, bytes):
                        try:
                            raw_line = raw_line.decode("utf-8", errors="strict")
                        except UnicodeDecodeError as exc:
                            raise ChatInvalidResponse("openai_compat_stream_invalid_utf8") from exc
                    if not raw_line.startswith("data:"):
                        continue
                    data = raw_line[5:].strip()
                    if data == "[DONE]":
                        metadata = dict(final_metadata)
                        if finish_reason:
                            metadata["finish_reason"] = finish_reason
                        yield ChatChunk(
                            done=True,
                            model=actual_model,
                            metadata=metadata,
                        )
                        saw_done = True
                        break
                    payload = _decode_json_line(data, source="openai_compat_stream")
                    if payload.get("error"):
                        raise ChatProviderError("openai_compat_provider_error")
                    actual_model = str(payload.get("model") or actual_model)
                    final_metadata.update(_openai_metadata(payload))
                    choices = payload.get("choices")
                    if isinstance(choices, list) and not choices:
                        continue
                    try:
                        choice = choices[0]
                        delta = choice.get("delta") or {}
                    except (KeyError, IndexError, TypeError) as exc:
                        raise ChatInvalidResponse("openai_compat_stream_missing_choice") from exc
                    text = delta.get("content") if isinstance(delta, dict) else None
                    if text is not None and not isinstance(text, str):
                        raise ChatInvalidResponse("openai_compat_stream_content_not_text")
                    if text:
                        yield ChatChunk(text=text, model=actual_model)
                    raw_finish_reason = choice.get("finish_reason")
                    if isinstance(raw_finish_reason, str):
                        finish_reason = raw_finish_reason
                if not saw_done:
                    raise ChatInvalidResponse("openai_compat_stream_missing_done")
            except requests.RequestException as exc:
                raise _translate_transport_error(exc) from exc
        finally:
            if response is not None:
                response.close()


class FallbackChatProvider:
    def __init__(
        self,
        *,
        name: str,
        primary: ChatProvider,
        fallback: ChatProvider,
        configured_fallback: str | None = None,
        should_fallback: Callable[[ChatProviderError], bool] | None = None,
    ) -> None:
        self.name = name
        self.primary = primary
        self.fallback = fallback
        self.configured_fallback = configured_fallback or fallback.name
        self.default_model = primary.default_model
        self.should_fallback = should_fallback or (lambda exc: True)

    def chat(self, messages: Sequence[Mapping[str, str]], *, model: str | None = None) -> ChatResult:
        try:
            result = self.primary.chat(messages, model=model)
            return ChatResult(
                result.text,
                result.model,
                self.name,
                {
                    **result.metadata,
                    "provider_requested": self.name,
                    "provider_effective": self.name,
                    "configured_fallback": self.configured_fallback,
                    "fallback_used": False,
                },
            )
        except ChatProviderError as exc:
            if not self.should_fallback(exc):
                raise
            fallback = self.fallback.chat(messages, model=None)
            metadata = {
                **fallback.metadata,
                "fallback_used": True,
                "configured_fallback": self.configured_fallback,
                "provider_requested": self.name,
                "provider_effective": fallback.provider,
                "fallback_from": self.name,
                "fallback_provider": fallback.provider,
                "fallback_reason": str(exc),
            }
            return ChatResult(fallback.text, fallback.model, fallback.provider, metadata)

    def stream_chat(self, messages: Sequence[Mapping[str, str]], *, model: str | None = None) -> Iterator[ChatChunk]:
        emitted = False
        try:
            for chunk in self.primary.stream_chat(messages, model=model):
                emitted = emitted or bool(chunk.text)
                if chunk.done:
                    yield ChatChunk(
                        text=chunk.text,
                        done=True,
                        model=chunk.model,
                        metadata={
                            **chunk.metadata,
                            "provider_requested": self.name,
                            "provider_effective": self.name,
                            "configured_fallback": self.configured_fallback,
                            "fallback_used": False,
                        },
                    )
                else:
                    yield chunk
            return
        except ChatProviderError as exc:
            if emitted or not self.should_fallback(exc):
                raise
            for chunk in self.fallback.stream_chat(messages, model=None):
                if chunk.done:
                    yield ChatChunk(
                        text=chunk.text,
                        done=True,
                        model=chunk.model,
                        metadata={
                            **chunk.metadata,
                            "fallback_used": True,
                            "configured_fallback": self.configured_fallback,
                            "provider_requested": self.name,
                            "provider_effective": self.fallback.name,
                            "fallback_from": self.name,
                            "fallback_provider": self.fallback.name,
                            "fallback_reason": str(exc),
                        },
                    )
                else:
                    yield chunk


def _selected_model(requested: str | None, default: str) -> str:
    selected = (requested or default).strip()
    if not selected:
        raise ChatProviderConfigurationError("empty_model")
    return selected


def _positive_env_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _runtime_config(config_path: str | Path | None = None) -> dict[str, Any]:
    env_config_path = os.getenv("RALF_INFERENCE_CONFIG")
    selected_path = Path(env_config_path or config_path or DEFAULT_RUNTIME_CONFIG)
    if selected_path.exists():
        with selected_path.open("r", encoding="utf-8") as handle:
            config = json.load(handle)
        if not isinstance(config, dict):
            raise ValueError("runtime config must be a JSON object")
    else:
        config = {}
    runtime_override = os.getenv("RALF_INFERENCE_RUNTIME", "").strip()
    if runtime_override:
        config = {**config, "default_runtime": runtime_override}
    return _with_chat_model_fallback(config)


def _with_chat_model_fallback(config: dict[str, Any]) -> dict[str, Any]:
    runtime_name = str(config.get("default_runtime") or "ollama")
    runtimes = config.get("runtimes")
    if not isinstance(runtimes, dict):
        return config
    runtime_config = runtimes.get(runtime_name)
    if not isinstance(runtime_config, dict):
        return config
    models = runtime_config.get("models")
    if not isinstance(models, dict) or models.get("chat") or models.get("default") or not models.get("planner"):
        return config
    updated_models = {**models, "chat": models["planner"]}
    updated_runtime = {**runtime_config, "models": updated_models}
    return {**config, "runtimes": {**runtimes, runtime_name: updated_runtime}}


def _runtime_values(config: dict[str, Any]) -> tuple[str, str, str]:
    runtime_name = str(config.get("default_runtime") or "ollama")
    runtimes = config.get("runtimes")
    runtime_config = runtimes.get(runtime_name, {}) if isinstance(runtimes, dict) else {}
    if not isinstance(runtime_config, dict):
        runtime_config = {}

    runtime_type = str(runtime_config.get("type") or runtime_name).lower()
    base_url = str(runtime_config.get("base_url") or "http://127.0.0.1:11434")
    models = runtime_config.get("models")
    model = "qwen2.5:7b"
    if isinstance(models, dict):
        model = str(models.get("chat") or models.get("default") or models.get("planner") or model)
    return runtime_type, base_url, model


def build_chat_provider(
    *,
    config_path: str | Path | None = None,
    connect_timeout_sec: float | None = None,
    inactivity_timeout_sec: float | None = None,
    session: requests.Session | None = None,
) -> ChatProvider:
    settings = ChatProviderSettings(
        connect_timeout_sec=connect_timeout_sec
        if connect_timeout_sec is not None
        else _positive_env_float("RALF_CHAT_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT_SEC),
        inactivity_timeout_sec=inactivity_timeout_sec
        if inactivity_timeout_sec is not None
        else _positive_env_float("RALF_CHAT_INACTIVITY_TIMEOUT", DEFAULT_INACTIVITY_TIMEOUT_SEC),
    )
    try:
        runtime_name, base_url, model = _runtime_values(_runtime_config(config_path))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ChatProviderConfigurationError("invalid_runtime_config") from exc

    if not base_url or not model:
        raise ChatProviderConfigurationError("runtime_missing_base_url_or_model")
    if runtime_name == "ollama":
        return OllamaChatProvider(
            base_url=base_url,
            model=model,
            settings=settings,
            session=session,
        )
    if runtime_name in {"openai_compat", "lmstudio"}:
        return OpenAICompatibleChatProvider(
            base_url=base_url,
            model=model,
            provider_name=runtime_name,
            settings=settings,
            session=session,
        )
    raise ChatProviderConfigurationError(f"unsupported_runtime:{runtime_name or 'unknown'}")


__all__ = [
    "ChatChunk",
    "ChatConnectTimeout",
    "ChatInactivityTimeout",
    "ChatInvalidResponse",
    "ChatProvider",
    "ChatProviderConfigurationError",
    "ChatProviderError",
    "ChatProviderHTTPError",
    "ChatProviderSettings",
    "ChatProviderUnavailable",
    "ChatResult",
    "FallbackChatProvider",
    "OllamaChatProvider",
    "OpenAICompatibleChatProvider",
    "build_chat_provider",
]
