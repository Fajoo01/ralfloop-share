from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    command: str
    path: str
    exit_code: int
    stdout: str | None = None
    stderr: str | None = None


class PatchEvidence(Evidence):
    diff: str
    tests: list[str] = Field(default_factory=list)


class CapabilityRoute(BaseModel):
    mode: Literal["check_only", "patch_allowed", "external_action"]
    reasoning: str
    skills_used: list[str] = Field(default_factory=list)
    mcp_used: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False


class TaskRequest(BaseModel):
    user_goal: str
    mode: str | None = None


class TaskResponse(BaseModel):
    route: CapabilityRoute
    evidence: Evidence | PatchEvidence | None = None
    message: str
    pending_confirmation_id: str | None = None
