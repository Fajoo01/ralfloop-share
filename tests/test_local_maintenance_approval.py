from __future__ import annotations

from ralfloop_agent.domains.domain_approval import DomainApprovalDecision, DomainApprovalPolicy
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from ralfloop_agent.integration.local_maintenance import (
    CANONICAL_ACTIONS,
    CanonicalLocalAction,
    CommandResult,
    LocalMaintenanceApprovalService,
    canonical_action_for_goal,
)


ACTION_ID = "systemd.restart.bottazzi_browser_bridge"


def _policy(tmp_path):
    return DomainApprovalPolicy(
        enabled=True,
        ttl_sec=300,
        max_pending=20,
        allowed_user_ids={11},
        allowed_chat_ids={22},
        require_private_chat=True,
        db_path=str(tmp_path / "approval.sqlite"),
        audit_log=str(tmp_path / "audit.jsonl"),
    )


class Runner:
    def __init__(self, *, apply_returncode=0, raise_apply=False):
        self.apply_returncode = apply_returncode
        self.raise_apply = raise_apply
        self.calls = []

    def __call__(self, argv):
        argv = tuple(argv)
        self.calls.append(argv)
        if argv[:2] == ("systemctl", "restart"):
            if self.raise_apply:
                raise OSError("systemd_unavailable")
            return CommandResult(self.apply_returncode, "", "failed" if self.apply_returncode else "")
        return CommandResult(0, "active\n", "")


def _request_and_approve(service, store):
    created = service.request(ACTION_ID, requested_by="test")
    request = created["request"]
    approved = store.decide(
        DomainApprovalDecision(
            request_id=request["request_id"],
            decision="approve",
            telegram_user_id=11,
            telegram_chat_id=22,
            telegram_message_id=33,
            idempotency_key="local-maintenance-approve-once",
        ),
        scope_digest_short=request["scope_digest_short"],
    )
    assert approved["status"] == "approved"
    return request["request_id"]


def test_preview_and_check_are_read_only_and_hash_bound(tmp_path):
    policy = _policy(tmp_path)
    runner = Runner()
    service = LocalMaintenanceApprovalService(
        DomainApprovalStore(policy=policy), policy=policy, runner=runner
    )

    preview = service.preview(ACTION_ID)

    assert preview["status"] == "preview"
    assert preview["approval_required"] is True
    assert preview["arbitrary_shell"] is False
    assert len(preview["binding_sha256"]) == 64
    assert len(preview["scope_digest"]) == 64
    assert runner.calls == [CANONICAL_ACTIONS[ACTION_ID].check_argv]


def test_approved_apply_is_persistent_one_shot_and_replay_safe(tmp_path):
    policy = _policy(tmp_path)
    store = DomainApprovalStore(policy=policy)
    runner = Runner()
    service = LocalMaintenanceApprovalService(store, policy=policy, runner=runner)
    request_id = _request_and_approve(service, store)

    first = service.apply(request_id)
    restarted_service = LocalMaintenanceApprovalService(
        DomainApprovalStore(policy=policy), policy=policy, runner=runner
    )
    replay = restarted_service.apply(request_id)

    assert first["status"] == "executed"
    assert first["verified"] is True
    assert replay["status"] == "already_executed"
    assert store.get_request(request_id)["status"] == "consumed"
    assert runner.calls.count(CANONICAL_ACTIONS[ACTION_ID].apply_argv) == 1


def test_canonical_action_mutation_invalidates_approval(tmp_path):
    policy = _policy(tmp_path)
    store = DomainApprovalStore(policy=policy)
    runner = Runner()
    service = LocalMaintenanceApprovalService(store, policy=policy, runner=runner)
    request_id = _request_and_approve(service, store)
    mutated = dict(CANONICAL_ACTIONS)
    mutated[ACTION_ID] = CanonicalLocalAction(
        action_id=ACTION_ID,
        apply_argv=("systemctl", "restart", "different.service"),
        check_argv=("systemctl", "is-active", "different.service"),
        target="different.service",
        expected_after=("active",),
    )

    result = LocalMaintenanceApprovalService(
        store, policy=policy, runner=runner, actions=mutated
    ).apply(request_id)

    assert result["status"] == "stale"
    assert store.get_request(request_id)["status"] == "stale"
    assert ("systemctl", "restart", "different.service") not in runner.calls


def test_apply_failure_is_fail_closed_and_never_retried(tmp_path):
    policy = _policy(tmp_path)
    store = DomainApprovalStore(policy=policy)
    runner = Runner(raise_apply=True)
    service = LocalMaintenanceApprovalService(store, policy=policy, runner=runner)
    request_id = _request_and_approve(service, store)

    failed = service.apply(request_id)
    replay = service.apply(request_id)

    assert failed["status"] == "execution_failed"
    assert failed["retry_allowed"] is False
    assert replay["status"] == "execution_failed"
    assert runner.calls.count(CANONICAL_ACTIONS[ACTION_ID].apply_argv) == 1


def test_gate_and_action_allowlist_fail_closed(tmp_path):
    disabled = DomainApprovalPolicy(enabled=False, db_path=str(tmp_path / "disabled.sqlite"))
    service = LocalMaintenanceApprovalService(
        DomainApprovalStore(policy=disabled), policy=disabled, runner=Runner()
    )

    assert service.request(ACTION_ID, requested_by="test")["status"] == "approval_gate_disabled"
    try:
        service.preview("shell.arbitrary")
    except ValueError as exc:
        assert str(exc) == "unsupported_canonical_local_action"
    else:
        raise AssertionError("arbitrary action accepted")


def test_disabled_gate_blocks_previously_approved_apply(tmp_path):
    enabled = _policy(tmp_path)
    store = DomainApprovalStore(policy=enabled)
    runner = Runner()
    request_id = _request_and_approve(
        LocalMaintenanceApprovalService(store, policy=enabled, runner=runner), store
    )
    disabled = DomainApprovalPolicy(
        enabled=False,
        db_path=enabled.db_path,
        audit_log=enabled.audit_log,
    )

    result = LocalMaintenanceApprovalService(
        DomainApprovalStore(policy=disabled), policy=disabled, runner=runner
    ).apply(request_id)

    assert result["status"] == "approval_gate_disabled"
    assert CANONICAL_ACTIONS[ACTION_ID].apply_argv not in runner.calls


def test_natural_goal_maps_only_to_fixed_registry_actions():
    assert canonical_action_for_goal("riavvia Bot-tazzi browser bridge") == ACTION_ID
    assert canonical_action_for_goal("esegui systemctl daemon-reload") == "systemd.daemon_reload"
    assert canonical_action_for_goal("riavvia Google Workspace MCP broker") == "systemd.restart.google_workspace_mcp_broker"
    assert canonical_action_for_goal("riavvia Ralfloop backend") == "systemd.restart.ralfloop_backend"
    assert canonical_action_for_goal("esegui questo shell arbitrario") is None
