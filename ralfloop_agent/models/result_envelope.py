from __future__ import annotations

from pydantic import BaseModel, Field

from ralfloop_agent.integration.execution_provenance import ExecutionProvenance
from src.confirmation import PendingConfirmation
from src.models import CapabilityRoute, CollaborationBackend, Evidence, JuryPolicy, PatchEvidence, VerificationPolicy


class ResultEnvelope(BaseModel):
    route: CapabilityRoute
    evidence: Evidence | PatchEvidence | None = None
    confirmation: PendingConfirmation | None = None
    jury_policy: JuryPolicy | None = None
    collaboration_backend: CollaborationBackend | None = None
    verification_policy: VerificationPolicy | None = None
    collaboration_trace: dict | None = None
    human_confirmation: dict | None = None
    jury_trace: dict | None = None
    answer: str | None = None
    meta: dict = Field(default_factory=dict)
    provenance: ExecutionProvenance = Field(default_factory=ExecutionProvenance)
