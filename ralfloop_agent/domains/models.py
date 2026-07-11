from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DomainState = Literal[
    "missing",
    "draft",
    "validating",
    "validation_required",
    "approval_required",
    "active",
    "deprecated",
    "quarantined",
    "invalid",
]


@dataclass
class DomainSource:
    source_id: str
    title: str
    source_type: str
    uri_or_path: str
    publisher: str = ""
    version: str = ""
    published_at: str | None = None
    retrieved_at: str | None = None
    checksum: str = ""
    reliability: float = 0.0
    sections_used: list[str] = field(default_factory=list)
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainRule:
    rule_id: str
    description: str
    priority: int
    conditions: dict[str, Any]
    action: dict[str, Any]
    exceptions: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    effective_from: str | None = None
    effective_to: str | None = None
    confidence: float = 1.0
    blocking: bool = False
    enabled: bool = True
    rule_type: str = "source_derived"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainException:
    exception_id: str
    description: str
    conditions: dict[str, Any] = field(default_factory=dict)
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainDecisionTable:
    table_id: str
    description: str
    inputs: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainExample:
    example_id: str
    kind: str
    input: str
    expected: Any = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainTestCase:
    test_id: str
    input: str
    expected: Any
    rule_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainManifest:
    domain_id: str
    display_name: str
    description: str
    version: str
    state: DomainState
    created_at: str
    updated_at: str
    created_by: str
    approved_by: str | None = None
    approved_at: str | None = None
    parent_version: str | None = None
    languages: list[str] = field(default_factory=lambda: ["it"])
    scope: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)
    source_requirements: list[str] = field(default_factory=list)
    deterministic_capabilities: list[str] = field(default_factory=list)
    jury_capabilities: list[str] = field(default_factory=list)
    external_action_policy: str = "deny"
    rule_precedence: list[str] = field(default_factory=list)
    minimum_source_count: int = 1
    minimum_test_pass_rate: float = 1.0
    content_hash: str = ""
    schema_version: str = "1.0"
    aliases: list[str] = field(default_factory=list)
    jury_review_completed: bool = False
    red_team_completed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainResolution:
    status: str
    domain_id: str | None = None
    version: str | None = None
    confidence: float = 0.0
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reason_codes: list[str] = field(default_factory=list)
    requires_jury_for_domain_selection: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainBuildResult:
    ok: bool
    status: str
    domain_id: str | None = None
    version: str | None = None
    draft_path: str | None = None
    blocking_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    approval_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainValidationResult:
    valid: bool
    blocking_issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    test_results: dict[str, Any] = field(default_factory=dict)
    source_results: dict[str, Any] = field(default_factory=dict)
    jury_review: dict[str, Any] = field(default_factory=dict)
    ready_for_approval: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DomainPromotionResult:
    ok: bool
    status: str
    domain_id: str
    version: str
    active_path: str | None = None
    approval_required: bool = False
    blocking_issues: list[str] = field(default_factory=list)
    audit_event: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
