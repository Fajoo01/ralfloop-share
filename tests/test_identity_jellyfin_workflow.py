from __future__ import annotations

from tests.test_service_identity import current, link, service, verification

from ralfloop_agent.unified_assistant.service_identity import (
    IdentityJellyfinShadowWorkflow, IdentityLinkStore, PlanAction,
    VerificationOutcome,
)
from ralfloop_agent.unified_assistant.memory_service import MemoryService
from ralfloop_agent.unified_assistant.tiremm_admin import TiremmAdminV2
from tests.test_service_identity import NOW


class ArciFixture:
    def __init__(self, outcome=VerificationOutcome.VERIFIED_ELIGIBLE):
        self.outcome = outcome

    def verify_membership(self, user_id, club_id):
        return verification(self.outcome, identity=user_id)


class JellyfinFixture:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.lookups = []

    def list_users(self):
        if self.fail:
            raise RuntimeError("jellyfin_source_unavailable")
        return (current(user_id="jf-unlinked", username="same-human-name"),)

    def get_user(self, *, user_id=None, username=None):
        self.lookups.append((user_id, username))
        if self.fail:
            raise RuntimeError("jellyfin_source_unavailable")
        return current(user_id=user_id, username="linked")


def test_shadow_workflow_uses_exact_identity_link_only():
    jellyfin = JellyfinFixture()
    workflow = IdentityJellyfinShadowWorkflow(
        ArciFixture(), jellyfin, IdentityLinkStore((link(),)), service(),
    )
    plan = workflow.run(arci_member_id="member-1", club_id="TIREMM")
    assert plan.action is PlanAction.NOOP
    assert jellyfin.lookups == [("jf-1", None)]


def test_shadow_workflow_never_fuzzy_links_unlinked_user():
    jellyfin = JellyfinFixture()
    workflow = IdentityJellyfinShadowWorkflow(
        ArciFixture(), jellyfin, IdentityLinkStore(), service(),
    )
    plan = workflow.run(arci_member_id="member-1", club_id="TIREMM")
    assert plan.action is PlanAction.CREATE
    assert jellyfin.lookups == []
    assert plan.requires_approval is True


def test_arci_or_jellyfin_outage_never_disables_or_creates():
    arci_outage = IdentityJellyfinShadowWorkflow(
        ArciFixture(VerificationOutcome.SOURCE_UNAVAILABLE), JellyfinFixture(),
        IdentityLinkStore(), service(),
    ).run(arci_member_id="member-1", club_id="TIREMM")
    jellyfin_outage = IdentityJellyfinShadowWorkflow(
        ArciFixture(), JellyfinFixture(fail=True), IdentityLinkStore(), service(),
    ).run(arci_member_id="member-1", club_id="TIREMM")

    assert arci_outage.action is PlanAction.NO_ACTION
    assert jellyfin_outage.action is PlanAction.NO_ACTION


def test_shadow_workflow_persists_practice_timeline(tmp_path):
    with MemoryService(tmp_path / "memory.sqlite") as memory:
        admin = TiremmAdminV2(memory)
        workflow = IdentityJellyfinShadowWorkflow(
            ArciFixture(), JellyfinFixture(), IdentityLinkStore(), service(),
        )
        plan, practice = workflow.run_and_record(
            arci_member_id="member-1", club_id="TIREMM", admin=admin, now=NOW,
        )
        assert admin.get_practice(practice.practice_id) is not None
        assert admin.get_timeline(practice.practice_id)
        assert plan.action is PlanAction.CREATE
