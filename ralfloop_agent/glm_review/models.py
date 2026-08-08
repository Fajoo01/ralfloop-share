from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceItem(StrictModel):
    source_id: str = Field(min_length=1, max_length=200)
    location: str = Field(min_length=1, max_length=500)
    claim: str = Field(min_length=1, max_length=1000)
    excerpt: str = Field(min_length=1, max_length=1400)
    classification: Literal["fact", "hypothesis", "missing_data"] = "fact"


class ContextPacket(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=200)
    task_type: Literal["grant_review", "technical_review", "other"]
    goal: str = Field(min_length=1, max_length=2000)
    constraints: list[str] = Field(default_factory=list, max_length=50)
    draft: str = Field(default="", max_length=12000)
    requirements: list[str] = Field(default_factory=list, max_length=100)
    evidence: list[EvidenceItem] = Field(default_factory=list, max_length=50)
    budget_summary: dict[str, Any] = Field(default_factory=dict)
    known_gaps: list[str] = Field(default_factory=list, max_length=100)
    requested_review: list[str] = Field(default_factory=list, max_length=50)


class CriticalIssue(StrictModel):
    severity: Literal["high", "medium", "low"]
    issue: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)


class RecommendedChange(StrictModel):
    target: str = Field(min_length=1, max_length=200)
    change: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class GlmReview(StrictModel):
    schema_version: Literal[1] = 1
    task_id: str = Field(min_length=1, max_length=200)
    verdict: Literal["accept", "revise", "reject", "insufficient_evidence"]
    summary: str = Field(min_length=1, max_length=300)
    critical_issues: list[CriticalIssue] = Field(default_factory=list, max_length=3)
    recommended_changes: list[RecommendedChange] = Field(default_factory=list, max_length=3)
    missing_evidence: list[str] = Field(default_factory=list, max_length=5)
    risk_flags: list[str] = Field(default_factory=list, max_length=50)
    confidence: float = Field(ge=0.0, le=1.0)
    requires_human_approval: Literal[False] = False


class AdapterStatus(StrEnum):
    COMPLETED = "completed"
    DEFERRED_RESOURCE_BUSY = "deferred_resource_busy"
    TIMEOUT = "timeout"
    INVALID_OUTPUT = "invalid_output"
    FAILED = "failed"
    ORPHANED_ATTEMPT = "orphaned_attempt"


class AdapterEnvelope(StrictModel):
    ok: bool
    provider: Literal["colibri_glm"] = "colibri_glm"
    model: Literal["glm-5.2-colibri"] = "glm-5.2-colibri"
    status: AdapterStatus
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    result: dict[str, Any] = Field(default_factory=dict)
    prompt_artifact: str
    result_artifact: str
    launcher_log: str = ""
    error: str | None = None
    ngen: int | None = Field(default=None, ge=1, le=2048)
    timeout_seconds: int | None = Field(default=None, ge=1, le=14400)
    model_started: bool = False
    orphan: bool = False
    stale: bool = False


class ValidationFinding(StrictModel):
    severity: Literal["error", "warning", "info"]
    code: str
    message: str


class ValidationReport(StrictModel):
    schema_version: Literal[1] = 1
    ok: bool
    task_id: str
    artifact_version: int = Field(ge=1)
    findings: list[ValidationFinding] = Field(default_factory=list)
    checked: list[str] = Field(default_factory=list)
    schema_valid: bool = True
    proposal_complete: bool = False
    eligibility_valid: bool = False


PROTECTED_ACTIONS = {
    "send_email",
    "submit_application",
    "portal_upload",
    "sign",
    "publish",
    "modify_external_data",
    "delete_external_data",
}


TERMINAL_JOB_STATES = {"completed", "approved", "rejected", "dispatched", "cancelled", "failed", "invalid_input"}
RETRYABLE_JOB_STATES = {"deferred_resource_busy", "timeout", "invalid_output", "orphaned_attempt", "failed"}
