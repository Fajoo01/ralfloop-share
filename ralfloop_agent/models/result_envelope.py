from __future__ import annotations

from pydantic import BaseModel, Field

from src.confirmation import PendingConfirmation
from src.models import CapabilityRoute, Evidence, PatchEvidence


class ResultEnvelope(BaseModel):
    route: CapabilityRoute
    evidence: Evidence | PatchEvidence | None = None
    confirmation: PendingConfirmation | None = None
    answer: str | None = None
    meta: dict = Field(default_factory=dict)
