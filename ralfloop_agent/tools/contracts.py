from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field


class PolicyDecision(BaseModel):
    allowed: bool = True
    reason: str = "allowed"


class ToolResult(BaseModel):
    ok: bool = True
    tool_name: str
    duration_ms: int = 0
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    artifacts: list[str] = Field(default_factory=list)
    policy: PolicyDecision = Field(default_factory=PolicyDecision)
    error_type: str | None = None


MemoryKind = Literal["observation", "decision", "warning", "result"]
