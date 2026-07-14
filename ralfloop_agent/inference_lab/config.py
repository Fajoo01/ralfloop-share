from __future__ import annotations

from dataclasses import dataclass
import os


EXPERIMENTAL_PROVIDERS = (
    "llama_cpp",
    "remote_tool",
    "speculative_local",
    "speculative_remote",
)
SUPPORTED_PROVIDERS = ("ollama", *EXPERIMENTAL_PROVIDERS)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _positive_float(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class InferenceLabConfig:
    provider: str = "ollama"
    llama_cpp_base_url: str = "http://127.0.0.1:19091"
    llama_cpp_model: str = "qwen2.5:7b"
    remote_mini_enabled: bool = False
    remote_mini_base_url: str = ""
    remote_mini_timeout_sec: float = 10.0
    remote_mini_role: str = "tool"
    remote_allowed_hosts: tuple[str, ...] = ()
    remote_max_input_bytes: int = 16_384
    remote_max_output_bytes: int = 8_192
    remote_concurrency: int = 1
    speculative_enabled: bool = False
    llama_cpp_speculative_confirmed: bool = False
    speculative_draft_mode: str = "remote"
    speculative_draft_url: str = ""
    speculative_max_draft_tokens: int = 4
    speculative_timeout_ms: int = 1_000

    def __post_init__(self) -> None:
        if self.provider not in SUPPORTED_PROVIDERS:
            raise ValueError(f"unsupported_chat_provider:{self.provider}")
        if self.remote_mini_role != "tool":
            raise ValueError("remote_mini_role_must_be_tool")
        if self.speculative_draft_mode not in {"local", "remote"}:
            raise ValueError("invalid_speculative_draft_mode")
        if not 1 <= self.speculative_max_draft_tokens <= 8:
            raise ValueError("invalid_speculative_max_draft_tokens")
        if self.remote_max_input_bytes > 65_536 or self.remote_max_output_bytes > 32_768:
            raise ValueError("remote_payload_limit_too_large")

    @classmethod
    def from_env(cls, provider: str | None = None) -> "InferenceLabConfig":
        selected = (provider or os.getenv("RALF_CHAT_PROVIDER") or "ollama").strip().lower()
        allowed_hosts = tuple(
            part.strip().lower()
            for part in os.getenv("RALF_REMOTE_MINI_ALLOWED_HOSTS", "").split(",")
            if part.strip()
        )
        return cls(
            provider=selected,
            llama_cpp_base_url=os.getenv("RALF_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19091").rstrip("/"),
            llama_cpp_model=os.getenv("RALF_LLAMA_CPP_MODEL", "qwen2.5:7b").strip() or "qwen2.5:7b",
            remote_mini_enabled=_env_bool("RALF_REMOTE_MINI_ENABLED"),
            remote_mini_base_url=os.getenv("RALF_REMOTE_MINI_BASE_URL", "").rstrip("/"),
            remote_mini_timeout_sec=_positive_float("RALF_REMOTE_MINI_TIMEOUT", 10.0),
            remote_mini_role=os.getenv("RALF_REMOTE_MINI_ROLE", "tool").strip().lower(),
            remote_allowed_hosts=allowed_hosts,
            remote_max_input_bytes=_positive_int("RALF_REMOTE_MINI_MAX_INPUT_BYTES", 16_384),
            remote_max_output_bytes=_positive_int("RALF_REMOTE_MINI_MAX_OUTPUT_BYTES", 8_192),
            remote_concurrency=_positive_int("RALF_REMOTE_MINI_CONCURRENCY", 1),
            speculative_enabled=_env_bool("RALF_SPECULATIVE_ENABLED"),
            llama_cpp_speculative_confirmed=_env_bool("RALF_LLAMA_CPP_SPECULATIVE_CONFIRMED"),
            speculative_draft_mode=os.getenv("RALF_SPECULATIVE_DRAFT_MODE", "remote").strip().lower(),
            speculative_draft_url=os.getenv("RALF_SPECULATIVE_DRAFT_URL", "").rstrip("/"),
            speculative_max_draft_tokens=_positive_int("RALF_SPECULATIVE_MAX_DRAFT_TOKENS", 4),
            speculative_timeout_ms=_positive_int("RALF_SPECULATIVE_TIMEOUT_MS", 1_000),
        )
