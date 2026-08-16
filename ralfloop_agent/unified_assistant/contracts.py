from __future__ import annotations

import os
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]{0,95}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class PolicyClass(StrEnum):
    READ = "READ"
    AUTO_WRITE = "AUTO_WRITE"
    CONFIRM_WRITE = "CONFIRM_WRITE"
    PROTECTED = "PROTECTED"
    DENY = "DENY"


class MemoryNamespace(StrEnum):
    PERSONAL_RELATIONAL = "personal_relational"
    TIREMM = "tiremm"
    HOME = "home"
    INFRASTRUCTURE = "infrastructure"
    MEDIA = "media"
    GENERAL_PREFERENCES = "general_preferences"


class MemoryType(StrEnum):
    LONG_TERM = "long_term"
    EPISODIC = "episodic"
    DOCUMENT = "document"
    WORKING = "working"
    CONVERSATION = "conversation"
    PREFERENCE = "preference"


class MemoryProvenance(StrEnum):
    USER_STATEMENT = "user_statement"
    EMAIL = "email"
    DOCUMENT = "document"
    TOOL_READ = "tool_read"
    APPROVED_ACTION = "approved_action"
    DERIVED_CALCULATION = "derived_calculation"


class MemoryItem(StrictModel):
    id: Identifier
    namespace: MemoryNamespace
    memory_type: MemoryType
    subject: str = Field(min_length=1, max_length=240)
    slot: str = Field(default="fact", min_length=1, max_length=96)
    content: str = Field(min_length=1, max_length=4000)
    epistemic_kind: Literal[
        "fact", "observation", "reported_statement", "evidence", "event",
        "hypothesis", "derived_score", "preference",
    ] = "fact"
    timestamp: str = Field(min_length=1, max_length=64)
    provenance: MemoryProvenance
    certainty: Literal["verified", "reported", "uncertain", "hypothesis", "calculated"]
    source_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    current: bool = True
    supersedes: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=16)
    derived: bool = False

    @model_validator(mode="after")
    def preserve_epistemic_type(self) -> "MemoryItem":
        if self.certainty in {"hypothesis", "calculated"} and not self.derived:
            raise ValueError("inference_must_be_derived")
        if self.epistemic_kind in {"hypothesis", "derived_score"} and not self.derived:
            raise ValueError("inference_must_be_derived")
        if self.certainty == "hypothesis" and self.epistemic_kind != "hypothesis":
            raise ValueError("hypothesis_kind_required")
        if self.certainty == "calculated" and self.epistemic_kind != "derived_score":
            raise ValueError("derived_score_kind_required")
        if self.provenance is MemoryProvenance.DERIVED_CALCULATION and not self.derived:
            raise ValueError("derived_provenance_requires_derived")
        if self.memory_type is MemoryType.CONVERSATION and self.current is False:
            raise ValueError("conversation_state_is_not_superseded_memory")
        return self


class DomainSpec(StrictModel):
    id: Identifier
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    description: str = Field(min_length=1, max_length=600)
    supported_skills: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=32)
    allowed_memory_namespaces: tuple[MemoryNamespace, ...] = Field(default_factory=tuple)
    context_builder: str = Field(min_length=1, max_length=240)
    policy: str = Field(min_length=1, max_length=240)
    validators: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    specialist_capabilities: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    status: Literal["ready", "constrained", "experimental"]
    source_refs: tuple[str, ...] = Field(default_factory=tuple, min_length=1, max_length=16)


class SkillSpec(StrictModel):
    id: Identifier
    domains: tuple[Identifier, ...] = Field(min_length=1, max_length=16)
    required_arguments: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    required_capabilities: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    classification: PolicyClass
    workflow: str = Field(min_length=1, max_length=240)
    verification_method: str = Field(min_length=1, max_length=240)
    status: Literal["ready", "constrained", "experimental"]


class UnifiedToolSpec(StrictModel):
    id: str = Field(min_length=1, max_length=160)
    capabilities: tuple[str, ...] = Field(min_length=1, max_length=32)
    input_schema: str = Field(min_length=1, max_length=160)
    output_schema: str = Field(min_length=1, max_length=160)
    classification: PolicyClass
    side_effect_class: str = Field(min_length=1, max_length=96)
    availability: str = Field(min_length=1, max_length=96)
    health: str = Field(min_length=1, max_length=160)
    verification_method: str = Field(min_length=1, max_length=240)
    source_registry: str = Field(min_length=1, max_length=240)


class PlanAssignment(StrictModel):
    task_id: Identifier
    domain: Identifier
    skill: Identifier
    objective: str = Field(min_length=1, max_length=1000)
    arguments: dict[str, Any] = Field(default_factory=dict)
    input_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    output_ref: str = Field(min_length=1, max_length=160)
    depends_on: tuple[Identifier, ...] = Field(default_factory=tuple, max_length=16)
    policy: PolicyClass
    content_is_data: bool = True


class AssistantPlan(StrictModel):
    schema_version: Literal["unified_assistant_plan_v1"] = "unified_assistant_plan_v1"
    intent: Identifier
    domains: tuple[Identifier, ...] = Field(min_length=1, max_length=8)
    assignments: tuple[PlanAssignment, ...] = Field(min_length=1, max_length=8)
    requires_clarification: bool = False
    clarification_reason: str | None = Field(default=None, max_length=240)


class AssistantFeatureFlags(StrictModel):
    unified_assistant: bool = False
    email_assistant_live: bool = False
    whatsapp_assistant_live: bool = False
    home_assistant_read_live: bool = False
    home_assistant_live: bool = False
    semantic_judge_enabled: bool = False
    semantic_judge_allow_normal: bool = False
    semantic_judge_shadow: bool = False

    @classmethod
    def from_env(cls) -> "AssistantFeatureFlags":
        return cls(
            unified_assistant=_env_bool("RALFLOOP_UNIFIED_ASSISTANT"),
            email_assistant_live=_env_bool("RALFLOOP_EMAIL_ASSISTANT_LIVE"),
            whatsapp_assistant_live=_env_bool("RALFLOOP_WHATSAPP_ASSISTANT_LIVE"),
            home_assistant_read_live=_env_bool("RALFLOOP_HOME_ASSISTANT_READ_LIVE"),
            home_assistant_live=_env_bool("RALFLOOP_HOME_ASSISTANT_LIVE"),
            semantic_judge_enabled=_env_bool("RALFLOOP_SEMANTIC_JUDGE_ENABLED"),
            semantic_judge_allow_normal=_env_bool("RALFLOOP_SEMANTIC_JUDGE_ALLOW_NORMAL"),
            semantic_judge_shadow=_env_bool("RALFLOOP_SEMANTIC_JUDGE_SHADOW"),
        )


def _env_bool(name: str) -> bool:
    return os.getenv(name, "0").strip().casefold() in {"1", "true", "yes", "on"}


def model_dict(value: BaseModel) -> dict[str, Any]:
    return value.model_dump(mode="json")


__all__ = [
    "AssistantFeatureFlags",
    "AssistantPlan",
    "DomainSpec",
    "MemoryItem",
    "MemoryNamespace",
    "MemoryProvenance",
    "MemoryType",
    "PlanAssignment",
    "PolicyClass",
    "SkillSpec",
    "UnifiedToolSpec",
    "model_dict",
]
