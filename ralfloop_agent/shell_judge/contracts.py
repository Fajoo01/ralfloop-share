from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ShellDecision(StrEnum):
    ALLOW_READONLY = "ALLOW_READONLY"
    ALLOW = "ALLOW"
    REVIEW = "REVIEW"
    DENY = "DENY"


class DestructiveLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    TOO_DESTRUCTIVE = "TOO_DESTRUCTIVE"


@dataclass(frozen=True)
class ShellCapability:
    executable: str
    argv: tuple[str, ...]
    cwd: str
    resolved_paths_read: tuple[str, ...] = ()
    resolved_paths_write: tuple[str, ...] = ()
    resolved_paths_delete: tuple[str, ...] = ()
    redirects: tuple[dict[str, object], ...] = ()
    env_assignments: tuple[str, ...] = ()
    substitutions: tuple[str, ...] = ()
    pipelines: int = 0
    subshells: int = 0
    background_execution: bool = False
    network_or_external_effects: bool = False
    unresolved_elements: tuple[str, ...] = ()
    destructive_level: DestructiveLevel = DestructiveLevel.LOW
    commands: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ShellReviewResult:
    decision: ShellDecision
    capabilities: ShellCapability
    deterministic_reason: str
    reviewer_required: bool
    risk: str = "unknown"
    authorization: str = "neutral"
    correctness: str = "unknown"
    rationale: tuple[str, ...] = field(default_factory=tuple)
