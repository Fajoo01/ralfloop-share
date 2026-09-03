from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import os
import re
import unicodedata
from typing import Iterable, Literal, Mapping, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class VerificationOutcome(StrEnum):
    VERIFIED_ELIGIBLE = "VERIFIED_ELIGIBLE"
    VERIFIED_INELIGIBLE = "VERIFIED_INELIGIBLE"
    NOT_FOUND = "NOT_FOUND"
    AMBIGUOUS = "AMBIGUOUS"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


class EligibilityDecision(StrEnum):
    ALLOW_CREATE = "ALLOW_CREATE"
    ALLOW_EXISTING = "ALLOW_EXISTING"
    DENY = "DENY"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ProvisioningState(StrEnum):
    UNLINKED = "UNLINKED"
    ACTIVE = "ACTIVE"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SUSPENDED = "SUSPENDED"


class JellyfinProvisioningConfig(StrictModel):
    mode: Literal["off", "shadow", "approved"] = "off"

    @classmethod
    def from_env(cls) -> "JellyfinProvisioningConfig":
        value = os.getenv("RALFLOOP_JELLYFIN_PROVISIONING", "off").strip().casefold()
        return cls(mode=value if value in {"off", "shadow", "approved"} else "off")


class PlanAction(StrEnum):
    CREATE = "CREATE"
    LINK = "LINK"
    NOOP = "NOOP"
    NO_ACTION = "NO_ACTION"
    REVIEW = "REVIEW"
    POSSIBLE_SUSPEND = "POSSIBLE_SUSPEND"


class JellyfinMutation(StrEnum):
    CREATE_USER = "CREATE_USER"
    ENABLE_USER = "ENABLE_USER"
    DISABLE_USER = "DISABLE_USER"
    UPDATE_POLICY = "UPDATE_POLICY"


class JellyfinExecutionGuard:
    """Pure guard only. This slice deliberately has no mutation transport."""

    @staticmethod
    def authorize(
        action: JellyfinMutation, config: JellyfinProvisioningConfig,
        *, approval_id: str | None,
    ) -> None:
        if config.mode != "approved":
            raise PermissionError("jellyfin_execution_mode_denied")
        if not approval_id or not approval_id.strip():
            raise PermissionError("jellyfin_execution_approval_required")
        if action not in set(JellyfinMutation):
            raise PermissionError("jellyfin_execution_capability_denied")


class SourceEvidence(StrictModel):
    source_type: Literal["arci_mcp", "jellyfin_api", "runtsuite_api", "legacy_file"]
    source_id: str = Field(min_length=1, max_length=240)
    locator: str = Field(min_length=1, max_length=1000)
    observed_at: datetime
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ArciMemberVerification(StrictModel):
    outcome: VerificationOutcome
    stable_member_id: str | None = Field(default=None, max_length=240)
    card_status: str | None = Field(default=None, max_length=80)
    campaign_year: int | None = Field(default=None, ge=2000, le=2200)
    organization_code: str | None = Field(default=None, max_length=120)
    display_name: str | None = Field(default=None, max_length=240)
    evidence: tuple[SourceEvidence, ...] = Field(default_factory=tuple, max_length=8)
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def eligible_requires_identity_and_evidence(self) -> "ArciMemberVerification":
        if self.outcome in {VerificationOutcome.VERIFIED_ELIGIBLE, VerificationOutcome.VERIFIED_INELIGIBLE}:
            if not self.stable_member_id or not self.evidence:
                raise ValueError("verified_arci_identity_evidence_required")
        return self


class JellyfinUserState(StrictModel):
    available: bool
    user_id: str | None = Field(default=None, max_length=240)
    username: str | None = Field(default=None, max_length=240)
    enabled: bool | None = None
    policy_fingerprint: str | None = Field(default=None, max_length=128)
    evidence: tuple[SourceEvidence, ...] = Field(default_factory=tuple, max_length=8)


class IdentityLink(StrictModel):
    local_identity_id: str = Field(pattern=r"^identity\.[a-z0-9][a-z0-9_.-]{0,86}$")
    arci_member_stable_id: str = Field(min_length=1, max_length=240)
    jellyfin_user_id: str | None = Field(default=None, max_length=240)
    runtsuite_identity_id: str | None = Field(default=None, max_length=240)
    created_at: datetime
    updated_at: datetime
    last_arci_verification_at: datetime
    provisioning_state: ProvisioningState
    source_refs: tuple[str, ...] = Field(min_length=1, max_length=16)


class IdentityLinkStore:
    def __init__(self, links: Iterable[IdentityLink] = (), *, max_links: int = 100_000) -> None:
        self.max_links = max_links
        self._links: dict[str, IdentityLink] = {}
        self._arci: dict[str, str] = {}
        self._jellyfin: dict[str, str] = {}
        self._runtsuite: dict[str, str] = {}
        for link in links:
            self.put(link)

    def put(self, link: IdentityLink) -> IdentityLink:
        if link.local_identity_id not in self._links and len(self._links) >= self.max_links:
            raise ValueError("identity_link_quota_exceeded")
        self._assert_unique(self._arci, link.arci_member_stable_id, link.local_identity_id, "arci")
        if link.jellyfin_user_id:
            self._assert_unique(self._jellyfin, link.jellyfin_user_id, link.local_identity_id, "jellyfin")
        if link.runtsuite_identity_id:
            self._assert_unique(self._runtsuite, link.runtsuite_identity_id, link.local_identity_id, "runtsuite")
        previous = self._links.get(link.local_identity_id)
        if previous and link.updated_at < previous.updated_at:
            raise ValueError("identity_link_stale")
        self._links[link.local_identity_id] = link
        self._arci[link.arci_member_stable_id] = link.local_identity_id
        if link.jellyfin_user_id:
            self._jellyfin[link.jellyfin_user_id] = link.local_identity_id
        if link.runtsuite_identity_id:
            self._runtsuite[link.runtsuite_identity_id] = link.local_identity_id
        return link

    def by_arci(self, stable_id: str) -> IdentityLink | None:
        identity = self._arci.get(stable_id)
        return self._links.get(identity) if identity else None

    @staticmethod
    def _assert_unique(index: dict[str, str], native_id: str, local_id: str, kind: str) -> None:
        if native_id in index and index[native_id] != local_id:
            raise ValueError(f"identity_link_{kind}_conflict")


class JellyfinEligibilityPolicy(StrictModel):
    version: Literal["jellyfin-eligibility-v1"] = "jellyfin-eligibility-v1"
    eligible_card_statuses: tuple[str, ...] = ("VALID", "ACTIVE", "R")
    required_organization_code: str | None = None

    def decide(self, verification: ArciMemberVerification, current: JellyfinUserState) -> EligibilityDecision:
        if verification.outcome in {VerificationOutcome.SOURCE_UNAVAILABLE, VerificationOutcome.AMBIGUOUS}:
            return EligibilityDecision.NEEDS_REVIEW
        if verification.outcome in {VerificationOutcome.NOT_FOUND, VerificationOutcome.VERIFIED_INELIGIBLE}:
            return EligibilityDecision.DENY
        if verification.card_status not in self.eligible_card_statuses:
            return EligibilityDecision.DENY
        if self.required_organization_code and verification.organization_code != self.required_organization_code:
            return EligibilityDecision.DENY
        return EligibilityDecision.ALLOW_EXISTING if current.user_id else EligibilityDecision.ALLOW_CREATE


class ProvisioningPlan(StrictModel):
    plan_id: str
    arci_member_stable_id: str | None
    verification_outcome: VerificationOutcome
    policy_version: str
    current: JellyfinUserState
    desired_state: ProvisioningState
    action: PlanAction
    candidate_username: str | None = None
    evidence: tuple[SourceEvidence, ...]
    reason: str
    requires_approval: bool = True


def username_candidate(display_name: str, stable_id: str, occupied: set[str]) -> str:
    normalized = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
    base = re.sub(r"[^a-z0-9]+", ".", normalized.casefold()).strip(".") or "socio"
    base = base[:32]
    if base not in occupied:
        return base
    suffix = hashlib.sha256(stable_id.encode()).hexdigest()[:8]
    candidate = f"{base[:23]}.{suffix}"
    if candidate in occupied:
        raise ValueError("jellyfin_username_collision_unresolved")
    return candidate


class JellyfinProvisioningService:
    def __init__(self, policy: JellyfinEligibilityPolicy, *, mode: Literal["off", "shadow", "approved"] = "off") -> None:
        self.policy = policy
        self.mode = mode

    def plan(
        self, verification: ArciMemberVerification, current: JellyfinUserState,
        *, identity_link: IdentityLink | None = None,
        occupied_usernames: set[str] | None = None,
    ) -> ProvisioningPlan:
        evidence = (*verification.evidence, *current.evidence)
        decision = self.policy.decide(verification, current)
        action, desired, reason, username = PlanAction.NO_ACTION, ProvisioningState.REVIEW_REQUIRED, decision.value, None
        if not current.available:
            reason = "jellyfin_source_unavailable"
        elif decision is EligibilityDecision.ALLOW_CREATE:
            username = username_candidate(verification.display_name or "socio", verification.stable_member_id or "", occupied_usernames or set())
            action, desired, reason = PlanAction.CREATE, ProvisioningState.ACTIVE, "eligible_missing_account"
        elif decision is EligibilityDecision.ALLOW_EXISTING:
            desired = ProvisioningState.ACTIVE
            if identity_link is None:
                action, reason = PlanAction.LINK, "eligible_legacy_account_exact_match_requires_link"
            elif identity_link.jellyfin_user_id == current.user_id:
                action, reason = PlanAction.NOOP, "eligible_account_present_and_linked"
            else:
                action, desired, reason = PlanAction.REVIEW, ProvisioningState.REVIEW_REQUIRED, "identity_link_conflict"
        elif verification.outcome is VerificationOutcome.AMBIGUOUS:
            action, reason = PlanAction.REVIEW, "ambiguous_identity"
        elif decision is EligibilityDecision.DENY:
            action = PlanAction.REVIEW if current.user_id else PlanAction.NO_ACTION
            desired = ProvisioningState.REVIEW_REQUIRED
            reason = "ineligible_no_automatic_suspension"
        return ProvisioningPlan(
            plan_id="plan." + hashlib.sha256(
                f"{verification.stable_member_id}|{verification.outcome}|{current.user_id}|{self.policy.version}".encode()
            ).hexdigest()[:24],
            arci_member_stable_id=verification.stable_member_id,
            verification_outcome=verification.outcome,
            policy_version=self.policy.version,
            current=current, desired_state=desired, action=action,
            candidate_username=username, evidence=evidence, reason=reason,
        )

    def execute(self, _: ProvisioningPlan) -> None:
        raise PermissionError(f"jellyfin_provisioning_{self.mode}_execution_disabled")


class MembershipVerifier(Protocol):
    def verify_membership(self, user_id: str, club_id: str) -> ArciMemberVerification: ...


class JellyfinIdentityReader(Protocol):
    def list_users(self) -> tuple[JellyfinUserState, ...]: ...
    def get_user(self, *, user_id: str | None = None, username: str | None = None) -> JellyfinUserState: ...


class IdentityJellyfinShadowWorkflow:
    """Exact native-ID orchestration. Never fuzzy-links or executes mutations."""

    def __init__(
        self, verifier: MembershipVerifier, jellyfin: JellyfinIdentityReader,
        links: IdentityLinkStore, provisioning: JellyfinProvisioningService,
    ) -> None:
        self.verifier = verifier
        self.jellyfin = jellyfin
        self.links = links
        self.provisioning = provisioning

    def run(self, *, arci_member_id: str, club_id: str) -> ProvisioningPlan:
        verification = self.verifier.verify_membership(arci_member_id, club_id)
        link = self.links.by_arci(arci_member_id)
        try:
            if link and link.jellyfin_user_id:
                current = self.jellyfin.get_user(user_id=link.jellyfin_user_id)
            else:
                self.jellyfin.list_users()  # health/completeness only; no fuzzy match
                current = JellyfinUserState(available=True)
        except Exception:
            current = JellyfinUserState(available=False)
        return self.provisioning.plan(
            verification, current, identity_link=link,
        )

    def run_and_record(
        self, *, arci_member_id: str, club_id: str, admin: object,
        now: datetime,
    ) -> tuple[ProvisioningPlan, object]:
        plan = self.run(arci_member_id=arci_member_id, club_id=club_id)
        records, practice = provisioning_plan_practice(plan, now=now)
        ingest = getattr(admin, "ingest_snapshot", None)
        project = getattr(admin, "project", None)
        if not callable(ingest) or not callable(project):
            raise TypeError("tiremm_admin_persistence_required")
        ingest(records)
        projected = project(practice)
        return plan, projected


class CurrentArciMcpMemberSource:
    """Truthful adapter: installed MCP has no individual member query."""

    def verify(self, stable_member_id: str) -> ArciMemberVerification:
        reason = "installed_arci_mcp_individual_member_query_not_exposed"
        return ArciMemberVerification(
            outcome=VerificationOutcome.SOURCE_UNAVAILABLE,
            stable_member_id=stable_member_id or None,
            evidence=(SourceEvidence(
                source_type="arci_mcp", source_id="arci-mcp-capability-v1",
                locator="tools/list", observed_at=datetime.now(timezone.utc),
                content_hash=hashlib.sha256(reason.encode()).hexdigest(),
            ),),
            reason=reason,
        )


class CurrentJellyfinMcpUserSource:
    """Truthful adapter: installed MCP exposes library tools, not user state."""

    def get_state(self, _: str) -> JellyfinUserState:
        return JellyfinUserState(available=False)


class ReconciliationStatus(StrEnum):
    MATCH = "MATCH"
    ONLY_LEGACY = "ONLY_LEGACY"
    ONLY_ARCI = "ONLY_ARCI"
    CONFLICT = "CONFLICT"
    AMBIGUOUS = "AMBIGUOUS"


class LegacyIdentity(StrictModel):
    stable_member_id: str | None = None
    legacy_key: str
    card_status: str | None = None


class ReconciliationRow(StrictModel):
    status: ReconciliationStatus
    stable_member_id: str | None
    legacy_keys: tuple[str, ...]
    reason: str


def reconcile_legacy(
    legacy: Iterable[LegacyIdentity], arci: Iterable[ArciMemberVerification],
) -> tuple[ReconciliationRow, ...]:
    legacy_by: dict[str, list[LegacyIdentity]] = {}
    unkeyed = []
    for row in legacy:
        if row.stable_member_id:
            legacy_by.setdefault(row.stable_member_id, []).append(row)
        else:
            unkeyed.append(row)
    arci_by = {row.stable_member_id: row for row in arci if row.stable_member_id}
    output = [ReconciliationRow(
        status=ReconciliationStatus.AMBIGUOUS, stable_member_id=None,
        legacy_keys=(row.legacy_key,), reason="legacy_row_without_stable_id_no_fuzzy_match",
    ) for row in unkeyed]
    for stable_id in sorted(set(legacy_by) | set(arci_by)):
        old = legacy_by.get(stable_id, [])
        current = arci_by.get(stable_id)
        if len(old) > 1:
            status, reason = ReconciliationStatus.AMBIGUOUS, "duplicate_legacy_stable_id"
        elif not current:
            status, reason = ReconciliationStatus.ONLY_LEGACY, "missing_from_arci_snapshot"
        elif not old:
            status, reason = ReconciliationStatus.ONLY_ARCI, "missing_from_legacy"
        elif old[0].card_status and current.card_status and old[0].card_status != current.card_status:
            status, reason = ReconciliationStatus.CONFLICT, "card_status_mismatch"
        else:
            status, reason = ReconciliationStatus.MATCH, "stable_id_exact"
        output.append(ReconciliationRow(
            status=status, stable_member_id=stable_id,
            legacy_keys=tuple(row.legacy_key for row in old), reason=reason,
        ))
    return tuple(output)


def audit_record(plan: ProvisioningPlan, *, actor: str, timestamp: datetime) -> dict[str, object]:
    return {
        "timestamp": timestamp.isoformat(),
        "arci_source_ids": [row.source_id for row in plan.evidence if row.source_type == "arci_mcp"],
        "verification_outcome": plan.verification_outcome,
        "eligibility_policy_version": plan.policy_version,
        "jellyfin_current_state": {
            "available": plan.current.available, "user_id": plan.current.user_id,
            "enabled": plan.current.enabled, "policy_fingerprint": plan.current.policy_fingerprint,
        },
        "desired_state": plan.desired_state, "action": plan.action,
        "actor": actor, "approval": None, "result": "shadow",
    }


def provisioning_plan_practice(plan: ProvisioningPlan, *, now: datetime):
    from .tiremm_admin import (
        NextAction, Practice, PracticeKind, PracticePriority, PracticeStatus,
        SourceKind, SourceRecord, SourcedFact,
    )

    records = []
    for row in plan.evidence:
        kind = {
            "arci_mcp": SourceKind.ARCI,
            "jellyfin_api": SourceKind.JELLYFIN,
            "runtsuite_api": SourceKind.RUNTSUITE,
            "legacy_file": SourceKind.DOCUMENT,
        }[row.source_type]
        records.append(SourceRecord(
            source_kind=kind, source_id=row.source_id, observed_at=row.observed_at,
            title=f"{row.source_type} verification",
            content=json.dumps({
                "content_hash": row.content_hash, "locator": row.locator,
                "plan_id": plan.plan_id, "outcome": plan.verification_outcome,
                "action": plan.action,
            }, sort_keys=True),
            location=row.locator,
            metadata={"trust": "authenticated", "freshness": "current"},
        ))
    evidence_ids = tuple(row.evidence_id for row in records)
    issue = plan.action in {PlanAction.REVIEW, PlanAction.NO_ACTION, PlanAction.POSSIBLE_SUSPEND}
    status = PracticeStatus.BLOCKED if issue else (
        PracticeStatus.COMPLETED if plan.action is PlanAction.NOOP else PracticeStatus.OPEN
    )
    next_action = None if status is PracticeStatus.COMPLETED else NextAction(
        action_type="review", description=f"Review shadow Jellyfin plan {plan.plan_id}",
        requires_approval=True, evidence_ids=evidence_ids,
    )
    blockers = (
        SourcedFact(field="jellyfin_access", value=plan.reason, evidence_ids=evidence_ids)
        if issue else None
    )
    practice = Practice(
        practice_id="practice.jellyfin-" + plan.plan_id.removeprefix("plan."),
        kind=PracticeKind.DIGITAL_SERVICE,
        title=f"Accesso Jellyfin {plan.arci_member_stable_id or 'unresolved'}",
        status=status, priority=PracticePriority.HIGH if issue else PracticePriority.NORMAL,
        opened_at=now, updated_at=now, responsible_party="Tiremm Innanz APS",
        evidence_ids=evidence_ids, next_action=next_action,
        blockers=(blockers,) if blockers else (),
    )
    return tuple(records), practice


RUNTSUITE_READ_CAPABILITIES = (
    "list_members", "list_member_cards", "list_member_account_links",
    "list_attendance", "list_projects", "list_funding_calls", "list_meetings",
    "list_movements", "list_supporting_documents", "list_project_documents",
    "list_accounts", "list_imports", "get_runts_review_queue",
    "get_mod_d_draft", "get_deposit_documents", "export_prima_nota",
)
RUNTSUITE_PROPOSE_CAPABILITIES = ("write_preview",)


__all__ = [
    "ArciMemberVerification", "CurrentArciMcpMemberSource", "CurrentJellyfinMcpUserSource",
    "EligibilityDecision", "IdentityJellyfinShadowWorkflow", "IdentityLink", "IdentityLinkStore", "JellyfinEligibilityPolicy",
    "JellyfinExecutionGuard", "JellyfinMutation",
    "JellyfinProvisioningConfig", "JellyfinProvisioningService", "JellyfinUserState", "LegacyIdentity", "PlanAction",
    "ProvisioningPlan", "ProvisioningState", "RUNTSUITE_PROPOSE_CAPABILITIES",
    "RUNTSUITE_READ_CAPABILITIES", "ReconciliationRow", "ReconciliationStatus",
    "SourceEvidence", "VerificationOutcome", "audit_record", "reconcile_legacy",
    "provisioning_plan_practice", "username_candidate",
]
