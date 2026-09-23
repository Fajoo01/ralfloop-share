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


class JuryPolicy(BaseModel):
    enabled: bool = False
    mode: Literal["off", "advisory", "required"] = "off"
    reason: str = "not_needed"
    triggers: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    requires_final_review: bool = False
    requires_human_confirmation: bool = False


class CollaborationBackend(BaseModel):
    backend_name: str = "single"
    implementation_level: Literal["routing_only", "text_proxy", "native_latent"] = "routing_only"
    style: str = "single"
    recursion_rounds: int = 0
    available: bool = True
    availability_reason: str = "available"
    native_latent: bool = False
    intermediate_decode_policy: str = "none"
    style_selection_source: str = "none"
    source: str = "ralfloop"
    requested_backend: str = "single"
    selected_backend: str = "single"
    fallback_used: bool = False
    fallback_reason: str | None = None
    checkpoints_downloaded: bool = False
    checkpoint_manifest_complete: bool = False
    offline_ready: bool = False
    load_check_passed: bool = False
    native_canary_passed: bool = False
    native_execution_verified: bool = False
    native_latent_verified: bool = False


class VerificationPolicy(BaseModel):
    enabled: bool = True
    verifier_type: Literal["deterministic", "rule", "llm_judge", "combined"] = "deterministic"
    criteria: list[str] = Field(default_factory=list)
    blocking: bool = True
    judge_provider: str | None = None


class LLMJudgeVerdict(BaseModel):
    candidate_answer: str
    user_goal: str
    criteria: list[str] = Field(default_factory=list)
    pass_: bool = Field(alias="pass")
    score: float | None = None
    reason: str
    judge_provider: str


JuryRoute = JuryPolicy


class CapabilityRoute(BaseModel):
    mode: Literal["check_only", "read_only_system_inspection", "patch_allowed", "external_action"]
    reasoning: str
    skills_used: list[str] = Field(default_factory=list)
    mcp_used: list[str] = Field(default_factory=list)
    requires_confirmation: bool = False
    jury_policy: JuryPolicy = Field(default_factory=JuryPolicy)
    collaboration_backend: CollaborationBackend = Field(default_factory=CollaborationBackend)
    verification_policy: VerificationPolicy = Field(default_factory=VerificationPolicy)
    adaptive_routing: dict | None = None
    jury: JuryPolicy = Field(default_factory=JuryPolicy)


class TaskRequest(BaseModel):
    user_goal: str
    mode: str | None = None


class TaskResponse(BaseModel):
    route: CapabilityRoute
    evidence: Evidence | PatchEvidence | None = None
    message: str
    pending_confirmation_id: str | None = None
    jury_policy: JuryPolicy | None = None
    collaboration_backend: CollaborationBackend | None = None
    verification_policy: VerificationPolicy | None = None
    collaboration_trace: dict | None = None
    human_confirmation: dict | None = None
    jury_trace: dict | None = None
