from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import subprocess
from typing import Any, Callable, Mapping, Sequence

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore


CAPABILITY = "local_software_maintenance"
APPROVAL_ACTION = "local_maintenance_apply"
SCHEMA_VERSION = "local_maintenance_v1"


@dataclass(frozen=True)
class CanonicalLocalAction:
    action_id: str
    apply_argv: tuple[str, ...]
    check_argv: tuple[str, ...]
    target: str
    expected_after: tuple[str, ...]


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Sequence[str]], CommandResult]


CANONICAL_ACTIONS: Mapping[str, CanonicalLocalAction] = {
    "systemd.daemon_reload": CanonicalLocalAction(
        action_id="systemd.daemon_reload",
        apply_argv=("systemctl", "daemon-reload"),
        check_argv=("systemctl", "show", "--property=NeedDaemonReload", "--value"),
        target="systemd-manager",
        expected_after=("no",),
    ),
    "systemd.restart.bottazzi_browser_bridge": CanonicalLocalAction(
        action_id="systemd.restart.bottazzi_browser_bridge",
        apply_argv=("systemctl", "restart", "bottazzi-browser-bridge.service"),
        check_argv=("systemctl", "is-active", "bottazzi-browser-bridge.service"),
        target="bottazzi-browser-bridge.service",
        expected_after=("active",),
    ),
    "systemd.restart.ralfloop_backend": CanonicalLocalAction(
        action_id="systemd.restart.ralfloop_backend",
        apply_argv=("systemctl", "restart", "ralfloop-backend.service"),
        check_argv=("systemctl", "is-active", "ralfloop-backend.service"),
        target="ralfloop-backend.service",
        expected_after=("active",),
    ),
    "systemd.restart.google_workspace_mcp_broker": CanonicalLocalAction(
        action_id="systemd.restart.google_workspace_mcp_broker",
        apply_argv=("systemctl", "restart", "ralf-google-workspace-mcp-broker.service"),
        check_argv=("systemctl", "is-active", "ralf-google-workspace-mcp-broker.service"),
        target="ralf-google-workspace-mcp-broker.service",
        expected_after=("active",),
    ),
}


def canonical_action_for_goal(user_goal: str) -> str | None:
    """Resolve only explicit, bounded systemd maintenance intents."""
    goal = user_goal.casefold()
    if "daemon-reload" in goal or "daemon reload" in goal:
        return "systemd.daemon_reload"
    restart = any(word in goal for word in ("riavvia", "restart", "riavvio"))
    if not restart:
        return None
    if any(word in goal for word in ("bottazzi", "bot-tazzi", "browser bridge")):
        return "systemd.restart.bottazzi_browser_bridge"
    if (
        "google workspace" in goal
        or "workspace mcp broker" in goal
        or "google-workspace-mcp" in goal
    ):
        return "systemd.restart.google_workspace_mcp_broker"
    if any(word in goal for word in ("ralfloop backend", "ralf backend", "ralfloop-backend")):
        return "systemd.restart.ralfloop_backend"
    return None


class LocalMaintenanceApprovalService:
    """Persistent approval for a fixed action registry; no shell text is accepted."""

    def __init__(
        self,
        store: DomainApprovalStore,
        *,
        policy: DomainApprovalPolicy,
        runner: Runner | None = None,
        actions: Mapping[str, CanonicalLocalAction] = CANONICAL_ACTIONS,
    ) -> None:
        self.store = store
        self.policy = policy
        self.runner = runner or _run_command
        self.actions = dict(actions)

    def check(self, action_id: str) -> dict[str, Any]:
        action = self._resolve(action_id)
        try:
            result = self.runner(action.check_argv)
        except Exception as exc:
            return {
                "status": "check_unavailable",
                "ok": False,
                "action_id": action_id,
                "error": type(exc).__name__,
            }
        output = result.stdout.strip()[:500]
        return {
            "status": "checked" if result.returncode == 0 else "check_failed",
            "ok": result.returncode == 0,
            "action_id": action_id,
            "target": action.target,
            "returncode": result.returncode,
            "output": output,
            "stderr": result.stderr.strip()[:500],
        }

    def preview(self, action_id: str) -> dict[str, Any]:
        action = self._resolve(action_id)
        binding = _binding(action)
        check = self.check(action_id)
        scope = _scope(action, check)
        return {
            "status": "preview",
            "capability": CAPABILITY,
            "action_id": action.action_id,
            "target": action.target,
            "canonical_action": list(action.apply_argv),
            "check": check,
            "binding_sha256": binding["binding_sha256"],
            "scope_digest": scope_digest(scope),
            "approval_required": True,
            "arbitrary_shell": False,
        }

    def request(self, action_id: str, *, requested_by: str) -> dict[str, Any]:
        if not self.policy.enabled:
            return {"status": "approval_gate_disabled", "approval_required": True}
        if not self.policy.allowed_user_ids or not self.policy.allowed_chat_ids:
            return {"status": "approval_allowlist_unconfigured", "approval_required": True}
        action = self._resolve(action_id)
        check = self.check(action_id)
        if not check.get("ok"):
            return {"status": "preview_check_failed", "check": check, "approval_required": True}
        return self.store.create_request(
            action=APPROVAL_ACTION,
            bando_id="local.software.maintenance",
            version=SCHEMA_VERSION,
            scope=_scope(action, check),
            requested_by=requested_by,
        )

    def apply(self, request_id: str) -> dict[str, Any]:
        if not self.policy.enabled:
            return {
                "status": "approval_gate_disabled",
                "request_id": request_id,
                "applied": False,
            }
        row = self.store.get_request(request_id)
        if not row:
            return {"status": "not_found", "request_id": request_id, "applied": False}
        effective = effective_approval_status(row)
        if effective == "consumed":
            return {"status": "already_executed", "request_id": request_id, "applied": False}
        if effective == "executing":
            return {"status": "execution_outcome_pending", "request_id": request_id, "applied": False}
        if effective == "execution_failed":
            return {"status": "execution_failed", "request_id": request_id, "applied": False, "retry_allowed": False}
        if effective != "approved":
            return {"status": "approval_required", "request_id": request_id, "applied": False}
        if row.get("action") != APPROVAL_ACTION:
            return {"status": "action_mismatch", "request_id": request_id, "applied": False}
        stored_scope = row.get("scope") if isinstance(row.get("scope"), Mapping) else {}
        action_id = str(stored_scope.get("action_id") or "")
        try:
            action = self._resolve(action_id)
        except ValueError:
            self.store.mark_stale(request_id, ["canonical_action_unavailable"])
            return {"status": "stale", "request_id": request_id, "applied": False}
        current_binding = _binding(action)
        if stored_scope.get("binding_sha256") != current_binding["binding_sha256"]:
            self.store.mark_stale(request_id, ["canonical_action_changed"])
            return {"status": "stale", "request_id": request_id, "applied": False}
        claim = self.store.claim_execution(request_id, action=APPROVAL_ACTION)
        if not claim.get("claimed"):
            return {
                "status": str(claim.get("status") or "execution_not_claimed"),
                "request_id": request_id,
                "applied": False,
            }
        try:
            executed = self.runner(action.apply_argv)
        except Exception as exc:
            return self._finish_failure(
                request_id, action, "canonical_action_unavailable_or_uncertain", type(exc).__name__
            )
        if executed.returncode != 0:
            return self._finish_failure(
                request_id, action, "canonical_action_failed", executed.stderr.strip()[:500]
            )
        verified = self.check(action_id)
        observed = str(verified.get("output") or "").casefold()
        if not verified.get("ok") or observed not in action.expected_after:
            return self._finish_failure(
                request_id, action, "post_check_failed", observed or str(verified.get("status"))
            )
        result = {
            "status": "executed",
            "request_id": request_id,
            "capability": CAPABILITY,
            "action_id": action.action_id,
            "target": action.target,
            "binding_sha256": current_binding["binding_sha256"],
            "applied": True,
            "verified": True,
            "retry_allowed": False,
        }
        finalized = self.store.finish_claimed_execution(
            request_id, action=APPROVAL_ACTION, success=True, result=result
        )
        if finalized.get("status") != "consumed":
            return {
                "status": "execution_finalization_failed",
                "request_id": request_id,
                "applied": False,
                "retry_allowed": False,
            }
        return result

    def _finish_failure(
        self, request_id: str, action: CanonicalLocalAction, reason: str, detail: str
    ) -> dict[str, Any]:
        result = {
            "status": "execution_failed",
            "request_id": request_id,
            "capability": CAPABILITY,
            "action_id": action.action_id,
            "target": action.target,
            "applied": False,
            "retry_allowed": False,
            "reason": reason,
            "detail": detail,
        }
        self.store.finish_claimed_execution(
            request_id, action=APPROVAL_ACTION, success=False, result=result
        )
        return result

    def _resolve(self, action_id: str) -> CanonicalLocalAction:
        try:
            return self.actions[action_id]
        except KeyError as exc:
            raise ValueError("unsupported_canonical_local_action") from exc


def _binding(action: CanonicalLocalAction) -> dict[str, Any]:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "capability": CAPABILITY,
        "action_id": action.action_id,
        "apply_argv": list(action.apply_argv),
        "check_argv": list(action.check_argv),
        "target": action.target,
        "expected_after": list(action.expected_after),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {**payload, "binding_sha256": hashlib.sha256(raw).hexdigest()}


def _scope(action: CanonicalLocalAction, check: Mapping[str, Any]) -> dict[str, Any]:
    binding = _binding(action)
    preview = {
        "status": str(check.get("status") or ""),
        "returncode": check.get("returncode"),
        "output_sha256": hashlib.sha256(str(check.get("output") or "").encode()).hexdigest(),
    }
    return {
        "action": APPROVAL_ACTION,
        "version": 1,
        "action_id": action.action_id,
        "target": action.target,
        "binding_sha256": binding["binding_sha256"],
        "binding": binding,
        "preview": preview,
    }


def _run_command(argv: Sequence[str]) -> CommandResult:
    completed = subprocess.run(
        list(argv), shell=False, text=True, capture_output=True, timeout=60, check=False
    )
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def register_local_maintenance_routes(app: Any) -> None:
    """Expose only registry IDs; request bodies can never provide commands."""
    policy = DomainApprovalPolicy.from_env()
    store = DomainApprovalStore(policy=policy)
    service = LocalMaintenanceApprovalService(store, policy=policy)

    @app.get("/local-maintenance/actions")
    async def list_local_maintenance_actions() -> dict[str, Any]:
        return {
            "status": "ok",
            "capability": CAPABILITY,
            "canonical_actions": sorted(CANONICAL_ACTIONS),
            "arbitrary_shell": False,
        }

    @app.get("/local-maintenance/actions/{action_id}/preview")
    async def preview_local_maintenance_action(action_id: str) -> dict[str, Any]:
        try:
            return service.preview(action_id)
        except ValueError:
            return {
                "status": "unsupported_canonical_local_action",
                "action_id": action_id,
                "approval_required": True,
            }

    @app.post("/local-maintenance/actions/{action_id}/requests")
    async def request_local_maintenance_action(
        action_id: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            return service.request(
                action_id,
                requested_by=str((payload or {}).get("requested_by") or "ralf"),
            )
        except ValueError:
            return {
                "status": "unsupported_canonical_local_action",
                "action_id": action_id,
                "approval_required": True,
            }

    @app.post("/local-maintenance/requests/{request_id}/apply")
    async def apply_local_maintenance_action(request_id: str) -> dict[str, Any]:
        return service.apply(request_id)


__all__ = [
    "APPROVAL_ACTION",
    "CANONICAL_ACTIONS",
    "CAPABILITY",
    "CanonicalLocalAction",
    "CommandResult",
    "LocalMaintenanceApprovalService",
    "canonical_action_for_goal",
    "register_local_maintenance_routes",
]
