from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Mapping

from ralfloop_agent.domains.domain_approval import (
    DomainApprovalDecision,
    DomainApprovalPolicy,
    effective_approval_status,
    scope_digest,
)
from ralfloop_agent.domains.domain_approval_store import DomainApprovalStore
from src.mcp_transport import MCPClientSession, MCPProtocolError, UnixMCPTransport

from .conversation import PendingAction, approval_matches, payload_matches


JELLYFIN_APPLY_ACTION = "jellyfin_apply_identity"


def _canonical_hash(value: Mapping[str, Any]) -> str:
    raw = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _provider_name(value: Any) -> str:
    folded = str(value or "").strip().casefold()
    if folded == "tmdb":
        return "Tmdb"
    if folded == "imdb":
        return "Imdb"
    raise ValueError("jellyfin_provider_invalid")


class JellyfinIdentityMCPProvider:
    def __init__(self, socket_path: str | None = None, *, timeout: float = 12.0) -> None:
        self.socket_path = socket_path or os.getenv(
            "RALF_JELLYFIN_MCP_SOCKET", "/run/ralf-jellyfin-mcp/mcp.sock"
        )
        self.timeout = timeout

    def _call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        with MCPClientSession(
            UnixMCPTransport(self.socket_path, connect_timeout=0.8),
            timeout=self.timeout,
            client_name="ralfloop-jellyfin-identity-write",
        ) as client:
            names = {item.name for item in client.list_tools()}
            if tool not in names:
                raise MCPProtocolError(f"jellyfin_tool_not_discovered:{tool}")
            result = client.call_tool(tool, dict(arguments))
        structured = result.get("structuredContent")
        if not isinstance(structured, Mapping):
            raise MCPProtocolError("jellyfin_tool_malformed")
        payload = dict(structured)
        if result.get("isError") or payload.get("ok") is False:
            raise MCPProtocolError(str(payload.get("status") or "jellyfin_tool_error"))
        return payload

    def read_identity(self, item_id: str) -> dict[str, Any]:
        return self._call("jellyfin_get_movie_identity", {"item_id": item_id})

    def search(self, item_id: str, *, name: str = "", year: int | None = None) -> dict[str, Any]:
        args: dict[str, Any] = {"item_id": item_id}
        if name:
            args["name"] = name
        if year:
            args["year"] = int(year)
        return self._call("jellyfin_search_movie_identity", args)

    def apply(self, scope: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("jellyfin_apply_movie_identity", {
            "item_id": str(scope["item_id"]),
            "provider": str(scope["provider"]),
            "provider_id": str(scope["provider_id"]),
            "name": str(scope.get("name") or ""),
            **({"year": int(scope["year"])} if scope.get("year") else {}),
            "confirm": True,
        })


def _candidate_rows(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("items") or []
    return [dict(item) for item in rows if isinstance(item, Mapping)]


def prepare_jellyfin_identity_payload(
    provider: JellyfinIdentityMCPProvider,
    arguments: Mapping[str, Any],
) -> dict[str, Any]:
    item_id = str(arguments.get("item_id") or "").strip()
    provider_name = _provider_name(arguments.get("provider"))
    provider_id = str(arguments.get("provider_id") or "").strip()
    if len(item_id) < 32 or not provider_id:
        raise ValueError("jellyfin_identity_scope_incomplete")

    before = provider.read_identity(item_id)
    search = provider.search(
        item_id,
        name=str(arguments.get("name") or before.get("name") or ""),
        year=int(arguments.get("year") or before.get("year") or 0) or None,
    )
    selected = next((
        row for row in _candidate_rows(search)
        if str((row.get("provider_ids") or {}).get(provider_name) or "") == provider_id
    ), None)
    if selected is None:
        raise ValueError("jellyfin_identity_not_in_search_results")

    before_state = {
        "item_id": str(before.get("item_id") or item_id),
        "name": str(before.get("name") or ""),
        "year": before.get("year"),
        "provider_ids": dict(before.get("provider_ids") or {}),
    }
    candidate = {
        "name": str(selected.get("name") or ""),
        "year": selected.get("year"),
        "provider_ids": dict(selected.get("provider_ids") or {}),
    }
    return {
        "item_id": item_id,
        "provider": provider_name,
        "provider_id": provider_id,
        "name": candidate["name"],
        "year": candidate["year"],
        "before_identity": before_state,
        "before_identity_sha256": _canonical_hash(before_state),
        "candidate_sha256": _canonical_hash(candidate),
    }


def build_jellyfin_identity_scope(pending: PendingAction) -> dict[str, Any]:
    if pending.domain != "jellyfin" or pending.action != JELLYFIN_APPLY_ACTION:
        raise ValueError("jellyfin_pending_required")
    payload = pending.payload
    scope = {
        "action": JELLYFIN_APPLY_ACTION,
        "version": 1,
        "pending_id": pending.pending_id,
        "pending_version": pending.version,
        "payload_digest": pending.payload_digest,
        "item_id": str(payload.get("item_id") or ""),
        "provider": _provider_name(payload.get("provider")),
        "provider_id": str(payload.get("provider_id") or ""),
        "name": str(payload.get("name") or ""),
        "year": payload.get("year"),
        "before_identity": dict(payload.get("before_identity") or {}),
        "before_identity_sha256": str(payload.get("before_identity_sha256") or ""),
        "candidate_sha256": str(payload.get("candidate_sha256") or ""),
    }
    if not all(scope[k] for k in (
        "item_id", "provider", "provider_id", "before_identity_sha256", "candidate_sha256"
    )):
        raise ValueError("jellyfin_scope_incomplete")
    return scope


class UnifiedJellyfinApprovalCoordinator:
    def __init__(self, store: DomainApprovalStore, *, policy: DomainApprovalPolicy) -> None:
        self.store = store
        self.policy = policy

    def request(self, pending: PendingAction, *, requested_by: str) -> dict[str, Any]:
        if not self.policy.enabled or not self.policy.allowed_user_ids or not self.policy.allowed_chat_ids:
            return {"status": "approval_gate_unavailable"}
        scope = build_jellyfin_identity_scope(pending)
        created = self.store.create_request(
            action=JELLYFIN_APPLY_ACTION,
            bando_id="jellyfin.identity",
            version=str(pending.version),
            scope=scope,
            requested_by=requested_by,
        )
        request = created.get("request") if isinstance(created, Mapping) else None
        if not isinstance(request, Mapping):
            return {"status": str(created.get("status") or "approval_request_failed")}
        return {
            "status": "pending",
            "request_id": str(request["request_id"]),
            "created_at": int(request["created_at"]),
            "expires_at": int(request["expires_at"]),
            "scope_digest_short": str(request["scope_digest_short"]),
        }

    def approve(
        self,
        pending: PendingAction,
        *,
        telegram_user_id: int,
        telegram_chat_id: int,
        telegram_message_id: int,
        chat_type: str = "private",
    ) -> dict[str, Any]:
        if not pending.approval_ref:
            return {"status": "approval_request_missing"}
        row = self.store.get_request(pending.approval_ref)
        if not row:
            return {"status": "approval_request_missing"}
        scope = build_jellyfin_identity_scope(pending)
        if str(row.get("scope_digest") or "") != scope_digest(scope):
            self.store.mark_stale(pending.approval_ref, ["jellyfin_identity_scope_changed"])
            return {"status": "scope_digest_mismatch"}
        return self.store.decide(
            DomainApprovalDecision(
                request_id=pending.approval_ref,
                decision="approve",
                telegram_user_id=telegram_user_id,
                telegram_chat_id=telegram_chat_id,
                telegram_message_id=telegram_message_id,
                chat_type=chat_type,
                idempotency_key=(
                    f"jellyfin:{pending.pending_id}:{pending.version}:{telegram_message_id}"
                ),
            ),
            scope_digest_short=str(row.get("scope_digest_short") or ""),
        )

    def cancel(self, pending: PendingAction) -> dict[str, Any]:
        if not pending.approval_ref:
            return {"status": "no_approval_request"}
        return self.store.cancel(pending.approval_ref)


class UnifiedJellyfinApprovalExecutor:
    def __init__(
        self,
        store: DomainApprovalStore,
        provider: JellyfinIdentityMCPProvider,
        *,
        write_enabled: bool = True,
    ) -> None:
        self.store = store
        self.provider = provider
        self.write_enabled = bool(write_enabled)

    def execute(self, pending: PendingAction) -> dict[str, Any]:
        if not payload_matches(pending) or not approval_matches(pending):
            return {"status": "APPROVAL_REQUIRED", "executed": False, "writes": 0}
        if pending.action != JELLYFIN_APPLY_ACTION:
            return {"status": "POLICY_DENIED", "executed": False, "writes": 0}
        request_id = str(pending.approval_ref or "")
        row = self.store.get_request(request_id)
        stored_status = str((row or {}).get("status") or "")
        if stored_status == "consumed":
            return {"status": "already_executed", "executed": True, "writes": 0,
                    "retry_allowed": False}
        if stored_status in {"executing", "execution_failed"}:
            return {"status": "EXECUTION_UNCERTAIN", "executed": False, "writes": 0,
                    "retry_allowed": False}
        if row is None or effective_approval_status(row) != "approved":
            return {"status": "APPROVAL_INVALID", "executed": False, "writes": 0}
        scope = build_jellyfin_identity_scope(pending)
        if str(row.get("scope_digest") or "") != scope_digest(scope):
            self.store.mark_stale(request_id, ["jellyfin_identity_scope_changed"])
            return {"status": "DRAFT_CHANGED", "executed": False, "writes": 0}

        try:
            before = self.provider.read_identity(str(scope["item_id"]))
        except Exception:
            return {"status": "SOURCE_UNAVAILABLE", "executed": False, "writes": 0,
                    "retry_allowed": True}
        observed_before = {
            "item_id": str(before.get("item_id") or scope["item_id"]),
            "name": str(before.get("name") or ""),
            "year": before.get("year"),
            "provider_ids": dict(before.get("provider_ids") or {}),
        }
        if _canonical_hash(observed_before) != str(scope["before_identity_sha256"]):
            self.store.mark_stale(request_id, ["jellyfin_identity_changed_before_apply"])
            return {"status": "DRAFT_CHANGED", "executed": False, "writes": 0,
                    "retry_allowed": False}

        try:
            search = self.provider.search(
                str(scope["item_id"]),
                name=str(scope.get("name") or ""),
                year=int(scope.get("year") or 0) or None,
            )
            still_valid = any(
                str((row.get("provider_ids") or {}).get(str(scope["provider"])) or "")
                == str(scope["provider_id"])
                for row in _candidate_rows(search)
            )
        except Exception:
            return {"status": "SOURCE_UNAVAILABLE", "executed": False, "writes": 0,
                    "retry_allowed": True}
        if not still_valid:
            self.store.mark_stale(request_id, ["jellyfin_candidate_disappeared"])
            return {"status": "DRAFT_CHANGED", "executed": False, "writes": 0,
                    "retry_allowed": False}
        if not self.write_enabled:
            return {"status": "jellyfin_write_disabled", "executed": False, "writes": 0,
                    "retry_allowed": True, "provider_call_attempted": False}

        claim = self.store.claim_execution(request_id, action=JELLYFIN_APPLY_ACTION)
        if not claim.get("claimed"):
            return {"status": str(claim.get("status") or "execution_claim_failed"),
                    "executed": False, "writes": 0, "retry_allowed": False}
        try:
            applied = self.provider.apply(scope)
            after = self.provider.read_identity(str(scope["item_id"]))
            provider_ids = dict(after.get("provider_ids") or {})
            if str(after.get("item_id") or "") != str(scope["item_id"]):
                raise RuntimeError("jellyfin_item_readback_mismatch")
            if str(provider_ids.get(str(scope["provider"])) or "") != str(scope["provider_id"]):
                raise RuntimeError("jellyfin_provider_readback_mismatch")
        except Exception as exc:
            result = {
                "status": "EXECUTION_UNCERTAIN", "executed": False,
                "writes": 1, "retry_allowed": False,
                "reason": type(exc).__name__,
            }
            self.store.finish_claimed_execution(
                request_id, action=JELLYFIN_APPLY_ACTION, success=False, result=result,
            )
            return result

        result = {
            "status": "EXECUTED_VERIFIED",
            "executed": True,
            "writes": 1,
            "retry_allowed": False,
            "item_id": str(scope["item_id"]),
            "provider": str(scope["provider"]),
            "provider_id": str(scope["provider_id"]),
            "provider_result": dict(applied),
            "provider_evidence": dict(after),
        }
        finalized = self.store.finish_claimed_execution(
            request_id, action=JELLYFIN_APPLY_ACTION, success=True, result=result,
        )
        if finalized.get("status") != "consumed":
            return {"status": "EXECUTION_UNCERTAIN", "executed": True, "writes": 1,
                    "retry_allowed": False}
        return result


__all__ = [
    "JELLYFIN_APPLY_ACTION",
    "JellyfinIdentityMCPProvider",
    "UnifiedJellyfinApprovalCoordinator",
    "UnifiedJellyfinApprovalExecutor",
    "build_jellyfin_identity_scope",
    "prepare_jellyfin_identity_payload",
]
