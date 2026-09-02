from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest
from pydantic import ValidationError

from ralfloop_agent.unified_assistant.service_identity import (
    ArciMemberVerification, CurrentArciMcpMemberSource, EligibilityDecision,
    IdentityLink, IdentityLinkStore, JellyfinEligibilityPolicy,
    JellyfinExecutionGuard, JellyfinMutation, JellyfinProvisioningConfig,
    JellyfinProvisioningService, JellyfinUserState, LegacyIdentity, PlanAction,
    ProvisioningState, ReconciliationStatus, SourceEvidence, VerificationOutcome,
    audit_record, provisioning_plan_practice, reconcile_legacy, username_candidate,
)
from ralfloop_agent.unified_assistant.tiremm_admin import TiremmAdminStore


NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


def evidence(kind="arci_mcp", identity="member-1"):
    return SourceEvidence(
        source_type=kind, source_id=identity, locator="fixture:source",
        observed_at=NOW, content_hash=hashlib.sha256(identity.encode()).hexdigest(),
    )


def verification(outcome=VerificationOutcome.VERIFIED_ELIGIBLE, *, identity="member-1", status="VALID"):
    return ArciMemberVerification(
        outcome=outcome,
        stable_member_id=identity if outcome not in {VerificationOutcome.NOT_FOUND, VerificationOutcome.AMBIGUOUS} else None,
        card_status=status, campaign_year=2026, organization_code="TIREMM",
        display_name="Mario Fixture", evidence=(evidence(identity=identity),), reason="fixture",
    )


def current(*, user_id=None, username=None, available=True, enabled=True):
    rows = (evidence("jellyfin_api", user_id),) if user_id else ()
    return JellyfinUserState(
        available=available, user_id=user_id, username=username,
        enabled=enabled if user_id else None, policy_fingerprint="abc" if user_id else None,
        evidence=rows,
    )


def service():
    return JellyfinProvisioningService(
        JellyfinEligibilityPolicy(required_organization_code="TIREMM"), mode="shadow"
    )


def link(jellyfin_user_id="jf-1"):
    return IdentityLink(
        local_identity_id="identity.one", arci_member_stable_id="member-1",
        jellyfin_user_id=jellyfin_user_id, created_at=NOW, updated_at=NOW,
        last_arci_verification_at=NOW, provisioning_state=ProvisioningState.ACTIVE,
        source_refs=("src:fixture",),
    )


def test_valid_member_without_account_creates_shadow_proposal():
    plan = service().plan(verification(), current())
    assert plan.action is PlanAction.CREATE
    assert plan.desired_state is ProvisioningState.ACTIVE
    assert plan.candidate_username == "mario.fixture"


def test_valid_member_with_account_is_noop():
    plan = service().plan(
        verification(), current(user_id="jf-1", username="mario.fixture"),
        identity_link=link(),
    )
    assert plan.action is PlanAction.NOOP


def test_exact_legacy_account_is_linked_not_duplicated():
    plan = service().plan(verification(), current(user_id="jf-1", username="mario.fixture"))
    assert plan.action is PlanAction.LINK
    assert plan.candidate_username is None


@pytest.mark.parametrize("outcome", [
    VerificationOutcome.SOURCE_UNAVAILABLE,
    VerificationOutcome.NOT_FOUND,
    VerificationOutcome.AMBIGUOUS,
])
def test_unverifiable_member_never_creates(outcome):
    result = verification(outcome)
    plan = service().plan(result, current())
    assert plan.action in {PlanAction.NO_ACTION, PlanAction.REVIEW}
    assert plan.action is not PlanAction.CREATE


def test_expired_card_never_auto_suspends_existing_account():
    plan = service().plan(
        verification(VerificationOutcome.VERIFIED_INELIGIBLE, status="EXPIRED"),
        current(user_id="jf-1", username="mario.fixture"),
    )
    assert plan.action is PlanAction.REVIEW
    assert plan.reason == "ineligible_no_automatic_suspension"


def test_jellyfin_outage_never_mutates():
    plan = service().plan(verification(), current(available=False))
    assert plan.action is PlanAction.NO_ACTION


def test_plan_and_retry_are_idempotent():
    first = service().plan(verification(), current())
    second = service().plan(verification(), current())
    assert first.plan_id == second.plan_id
    assert first == second
    with pytest.raises(PermissionError, match="shadow_execution_disabled"):
        service().execute(first)


def test_execution_guard_is_allowlisted_but_not_a_transport():
    with pytest.raises(PermissionError, match="mode_denied"):
        JellyfinExecutionGuard.authorize(
            JellyfinMutation.CREATE_USER, JellyfinProvisioningConfig(mode="shadow"),
            approval_id="approval.fixture",
        )
    with pytest.raises(PermissionError, match="approval_required"):
        JellyfinExecutionGuard.authorize(
            JellyfinMutation.CREATE_USER, JellyfinProvisioningConfig(mode="approved"),
            approval_id=None,
        )
    JellyfinExecutionGuard.authorize(
        JellyfinMutation.UPDATE_POLICY, JellyfinProvisioningConfig(mode="approved"),
        approval_id="approval.fixture",
    )
    assert "DELETE_USER" not in {item.value for item in JellyfinMutation}


def test_username_collision_is_stable_and_card_number_is_not_public():
    value = username_candidate("Mario Fixture", "ARCI-CARD-SECRET", {"mario.fixture"})
    assert value.startswith("mario.fixture.")
    assert "ARCI" not in value and "SECRET" not in value
    assert value == username_candidate("Mario Fixture", "ARCI-CARD-SECRET", {"mario.fixture"})


def test_identity_link_uses_only_native_ids_and_rejects_duplicates():
    link = IdentityLink(
        local_identity_id="identity.one", arci_member_stable_id="arci-1",
        jellyfin_user_id="jf-1", runtsuite_identity_id="42",
        created_at=NOW, updated_at=NOW, last_arci_verification_at=NOW,
        provisioning_state=ProvisioningState.ACTIVE, source_refs=("src:fixture",),
    )
    store = IdentityLinkStore((link,))
    assert store.by_arci("arci-1") == link
    with pytest.raises(ValueError, match="arci_conflict"):
        store.put(link.model_copy(update={"local_identity_id": "identity.two"}))
    assert "display_name" not in IdentityLink.model_fields


def test_legacy_reconciliation_has_all_required_states_without_fuzzy_matching():
    rows = reconcile_legacy(
        (
            LegacyIdentity(stable_member_id="a", legacy_key="legacy-a", card_status="VALID"),
            LegacyIdentity(stable_member_id="b", legacy_key="legacy-b", card_status="OLD"),
            LegacyIdentity(stable_member_id="c", legacy_key="legacy-c"),
            LegacyIdentity(legacy_key="no-stable-id"),
        ),
        (
            verification(identity="a"), verification(identity="b"), verification(identity="d"),
        ),
    )
    assert {row.status for row in rows} == {
        ReconciliationStatus.MATCH, ReconciliationStatus.CONFLICT,
        ReconciliationStatus.ONLY_LEGACY, ReconciliationStatus.ONLY_ARCI,
        ReconciliationStatus.AMBIGUOUS,
    }


def test_audit_and_models_never_contain_password_or_token():
    record = json.dumps(audit_record(service().plan(verification(), current()), actor="bottazzi", timestamp=NOW))
    assert "password" not in record.casefold()
    assert "token" not in record.casefold()


def test_plan_becomes_source_backed_tiremm_practice():
    plan = service().plan(verification(), current())
    records, practice = provisioning_plan_practice(plan, now=NOW)
    store = TiremmAdminStore()
    store.ingest_snapshot(records); store.project(practice)
    assert store.retrieve("Jellyfin member-1")[0].practice.practice_id == practice.practice_id
    assert store.get_sources(practice.practice_id)


def test_installed_arci_capability_truthfully_fails_closed():
    result = CurrentArciMcpMemberSource().verify("member-1")
    assert result.outcome is VerificationOutcome.SOURCE_UNAVAILABLE
    assert result.evidence


def test_verified_outcome_requires_source_and_stable_id():
    with pytest.raises(ValidationError):
        ArciMemberVerification(
            outcome=VerificationOutcome.VERIFIED_ELIGIBLE,
            card_status="VALID", reason="bad",
        )
