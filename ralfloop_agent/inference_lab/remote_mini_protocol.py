from __future__ import annotations

import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .security import reject_secrets, validate_non_binding_result


PROTOCOL_VERSION = "ralf-mini-v1"
ALLOWED_TASKS = {
    "intent_classification",
    "repo_context_selection",
    "fact_extraction",
    "git_status_summary",
    "history_compression",
    "rag_query_generation",
    "json_format_check",
    "response_precheck",
    "tool_suggestion",
}


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class MiniLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_input_bytes: int = Field(default=16_384, ge=256, le=65_536)
    max_output_bytes: int = Field(default=8_192, ge=256, le=32_768)
    timeout_ms: int = Field(default=10_000, ge=100, le=60_000)


class MiniToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["ralf-mini-v1"] = PROTOCOL_VERSION
    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    role: Literal["tool"] = "tool"
    task: str
    input: dict[str, Any]
    limits: MiniLimits = Field(default_factory=MiniLimits)

    @model_validator(mode="after")
    def validate_payload(self) -> "MiniToolRequest":
        if self.task not in ALLOWED_TASKS:
            raise ValueError("unsupported_remote_task")
        reject_secrets(self.input)
        if json_size(self.input) > self.limits.max_input_bytes:
            raise ValueError("remote_input_too_large")
        return self


class MiniTimings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queue_ms: float = Field(default=0, ge=0)
    prompt_ms: float = Field(default=0, ge=0)
    generation_ms: float = Field(default=0, ge=0)


class MiniToolResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["ralf-mini-v1"] = PROTOCOL_VERSION
    request_id: str = Field(min_length=8, max_length=64)
    ok: bool
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = Field(default=None, max_length=256)
    timings: MiniTimings = Field(default_factory=MiniTimings)

    @model_validator(mode="after")
    def validate_result(self) -> "MiniToolResponse":
        validate_non_binding_result(self.result)
        return self


class DraftRequest(BaseModel):
    """Separate protocol: a draft is token IDs, never textual suggestions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["ralf-draft-v1"] = "ralf-draft-v1"
    request_id: str = Field(default_factory=lambda: uuid4().hex, min_length=8, max_length=64)
    context_token_ids: list[int] = Field(min_length=1, max_length=131_072)
    max_draft_tokens: int = Field(default=4, ge=1, le=8)
    tokenizer_hash: str = Field(min_length=16, max_length=128)
    vocabulary_hash: str = Field(min_length=16, max_length=128)


class DraftResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: Literal["ralf-draft-v1"] = "ralf-draft-v1"
    request_id: str = Field(min_length=8, max_length=64)
    ok: bool
    draft_token_ids: list[int] = Field(default_factory=list, max_length=8)
    tokenizer_hash: str = Field(min_length=16, max_length=128)
    vocabulary_hash: str = Field(min_length=16, max_length=128)
    generation_ms: float = Field(default=0, ge=0)
