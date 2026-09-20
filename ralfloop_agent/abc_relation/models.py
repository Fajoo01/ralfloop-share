from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EvidenceKind(str, Enum):
    OBSERVED_FACT = "observed_fact"
    INFERENCE = "inference"
    CONTRADICTION = "contradiction"
    EXTERNAL_SIGNAL = "external_signal"
    OPERATIONAL_CONSTRAINT = "operational_constraint"
    BOUNDARY = "boundary"


class SourceKind(str, Enum):
    MANUAL = "manual"
    WHATSAPP_EXPORT = "whatsapp_export"
    CHATGPT_SNAPSHOT = "chatgpt_snapshot"
    LEGACY_ABC = "legacy_abc"
    IMPORTED_JSON = "imported_json"
    DERIVED = "derived"


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: SourceKind
    source_ref: str = Field(min_length=1, max_length=1000)
    source_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    imported_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    line_start: int | None = Field(default=None, ge=1)
    line_end: int | None = Field(default=None, ge=1)

    @field_validator("line_end")
    @classmethod
    def _line_end_valid(cls, value: int | None, info: Any) -> int | None:
        start = info.data.get("line_start")
        if value is not None and start is not None and value < start:
            raise ValueError("line_end_before_line_start")
        return value


class RelationEvent(BaseModel):
    """One atomic relational evidence record.

    `summary` is the normalized fact/inference used by the model. `raw_excerpt`
    is optional and intentionally capped so the DB does not become a shadow copy
    of private chat history.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(pattern=r"^abc_evt_[a-f0-9]{16}$")
    occurred_at: datetime
    actor: str | None = Field(default=None, max_length=120)
    kind: EvidenceKind
    summary: str = Field(min_length=1, max_length=2000)
    raw_excerpt: str | None = Field(default=None, max_length=600)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    weight: float = Field(default=0.0, ge=-100.0, le=100.0)
    tags: tuple[str, ...] = ()
    provenance: Provenance
    supersedes_event_id: str | None = Field(default=None, pattern=r"^abc_evt_[a-f0-9]{16}$")
    active: bool = True


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=1, max_length=2000)
    probability: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    supporting_event_ids: tuple[str, ...] = ()
    contradicting_event_ids: tuple[str, ...] = ()
    status: Literal["active", "weak", "rejected", "unknown"] = "active"


class StrategyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1, max_length=160)
    text: str = Field(min_length=1, max_length=2000)
    rationale: str | None = Field(default=None, max_length=2000)
    priority: int = Field(default=50, ge=0, le=100)
    enabled: bool = True


class RelationSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(pattern=r"^abc_snap_[a-f0-9]{16}$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    label: str = Field(min_length=1, max_length=300)
    state_summary: str = Field(min_length=1, max_length=5000)
    hypotheses: tuple[Hypothesis, ...] = ()
    strategy_rules: tuple[StrategyRule, ...] = ()
    warnings: tuple[str, ...] = ()
    source_refs: tuple[str, ...] = ()
    legacy_timeline: tuple[dict[str, Any], ...] = ()
    score_payload: dict[str, Any] = Field(default_factory=dict)


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relcalc: dict[str, Any]
    formula_loop: dict[str, Any] | None = None
    active_event_count: int
    observed_fact_count: int
    inference_count: int
    contradiction_count: int
    warnings: tuple[str, ...] = ()


class ImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    imported_events: int = 0
    imported_snapshots: int = 0
    skipped: int = 0
    warnings: tuple[str, ...] = ()
