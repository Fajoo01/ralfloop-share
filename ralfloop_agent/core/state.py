from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import uuid4
from pydantic import BaseModel, Field

from ralfloop_agent.tools.contracts import ToolResult


class SandboxState(BaseModel):
    id: str | None = None
    status: Literal["not_created", "ready", "destroyed", "error"] = "not_created"
    workspace_path: str = "/workspace"
    created_at: datetime | None = None
    destroyed_at: datetime | None = None


class PlanStep(BaseModel):
    step_id: str
    description: str
    status: Literal["pending", "done", "failed"] = "pending"


class MemoryEntry(BaseModel):
    kind: Literal["observation", "decision", "warning", "result"]
    content: str


class AgentState(BaseModel):
    task_id: str = Field(default_factory=lambda: str(uuid4()))
    user_goal: str
    constraints: list[str] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    iteration: int = 0
    max_iterations: int = 8
    status: Literal["idle", "running", "completed", "failed", "blocked"] = "idle"
    sandbox: SandboxState = Field(default_factory=SandboxState)
    plan: list[PlanStep] = Field(default_factory=list)
    memory: list[MemoryEntry] = Field(default_factory=list)
    last_action: dict[str, Any] | None = None
    last_result: ToolResult | None = None
    consecutive_failures: int = 0
    stop_reason: str | None = None
    final_answer: str | None = None
