from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Protocol
from urllib import error, request


@dataclass(frozen=True)
class RuntimeCapabilities:
    streaming: bool = False
    speculative_decoding: bool = False
    quantization: list[str] = field(default_factory=list)
    mmap_weights: bool = False
    gpu_offload: bool = False
    metrics: bool = False


@dataclass(frozen=True)
class GenerateRequest:
    model: str
    prompt: str
    stream: bool = False
    format: dict[str, Any] | str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    timeout_sec: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GenerateResult:
    text: str
    raw: dict[str, Any] = field(default_factory=dict)
    model: str | None = None
    runtime: str = "unknown"


class InferenceRuntime(Protocol):
    name: str
    capabilities: RuntimeCapabilities

    def generate(self, request: GenerateRequest) -> GenerateResult:
        ...


class OllamaRuntime:
    name = "ollama"
    capabilities = RuntimeCapabilities(streaming=True)

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:7b",
        timeout_sec: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec

    def generate(self, generate_request: GenerateRequest) -> GenerateResult:
        model = generate_request.model or self.model
        timeout_sec = generate_request.timeout_sec or self.timeout_sec
        payload: dict[str, Any] = {
            "model": model,
            "prompt": generate_request.prompt,
            "stream": generate_request.stream,
            "options": generate_request.options,
        }
        if generate_request.format is not None:
            payload["format"] = generate_request.format

        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout_sec) as resp:
                raw_body = resp.read().decode("utf-8")
        except error.URLError as exc:
            raise RuntimeError(f"Ollama unavailable: {exc}") from exc
        except TimeoutError as exc:
            raise RuntimeError("Ollama request timed out") from exc

        try:
            parsed = json.loads(raw_body)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Ollama returned invalid JSON") from exc

        text = parsed.get("response")
        if not isinstance(text, str):
            raise RuntimeError("Ollama response missing 'response'")
        return GenerateResult(text=text, raw=parsed, model=model, runtime=self.name)


class OpenAICompatRuntime:
    name = "openai_compat"
    capabilities = RuntimeCapabilities(streaming=True)

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        model: str = "local-model",
        timeout_sec: int = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_sec = timeout_sec

    def generate(self, generate_request: GenerateRequest) -> GenerateResult:
        model = generate_request.model or self.model
        timeout_sec = generate_request.timeout_sec or self.timeout_sec
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": generate_request.prompt}],
            "temperature": generate_request.options.get("temperature", 0),
            "stream": False,
        }
        if isinstance(generate_request.format, dict):
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "ralf_generate",
                    "strict": True,
                    "schema": generate_request.format,
                },
            }
        elif generate_request.format == "json":
            payload["response_format"] = {"type": "json_object"}
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            f"{self.base_url}/v1/chat/completions",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=timeout_sec) as resp:
                raw_body = resp.read().decode("utf-8")
        except error.URLError as exc:
            raise RuntimeError(f"OpenAI-compatible runtime unavailable: {exc}") from exc
        except TimeoutError as exc:
            raise RuntimeError("OpenAI-compatible request timed out") from exc

        try:
            parsed = json.loads(raw_body)
            text = parsed["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise RuntimeError("OpenAI-compatible runtime returned invalid response") from exc
        if not isinstance(text, str):
            raise RuntimeError("OpenAI-compatible response content is not text")
        return GenerateResult(text=text, raw=parsed, model=model, runtime=self.name)


class LMStudioRuntime(OpenAICompatRuntime):
    name = "lmstudio"

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:1234",
        model: str = "local-model",
        timeout_sec: int = 60,
    ) -> None:
        super().__init__(base_url=base_url, model=model, timeout_sec=timeout_sec)


def load_runtime_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        return {}
    with config_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Runtime config must be a JSON object")
    return data


def runtime_from_config(
    config: dict[str, Any],
    role: str = "planner",
    timeout_sec: int = 60,
) -> InferenceRuntime:
    runtime_name = str(config.get("default_runtime") or "ollama")
    runtimes = config.get("runtimes") or {}
    runtime_cfg = runtimes.get(runtime_name, {}) if isinstance(runtimes, dict) else {}
    if not isinstance(runtime_cfg, dict):
        runtime_cfg = {}

    runtime_type = str(runtime_cfg.get("type") or runtime_name).lower()
    base_url = str(runtime_cfg.get("base_url") or "http://127.0.0.1:11434")
    models = runtime_cfg.get("models") or {}
    model = "qwen2.5:7b"
    if isinstance(models, dict):
        model = str(models.get(role) or models.get("default") or model)

    if runtime_type == "lmstudio":
        return LMStudioRuntime(base_url=base_url, model=model, timeout_sec=timeout_sec)
    if runtime_type in {"openai_compat", "openai-compatible", "openai"}:
        return OpenAICompatRuntime(base_url=base_url, model=model, timeout_sec=timeout_sec)
    return OllamaRuntime(base_url=base_url, model=model, timeout_sec=timeout_sec)
