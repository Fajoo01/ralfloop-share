from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
import urllib.error
import urllib.request

from .service_identity import (
    ArciMemberVerification, CurrentArciMcpMemberSource, JellyfinUserState,
    SourceEvidence, VerificationOutcome,
)


MCP_PROTOCOL_VERSION = "2024-11-05"
ARCI_TOOL = "arci_verify_member_eligibility"
JELLYFIN_LIST_TOOL = "jellyfin_list_users_read_only"
JELLYFIN_GET_TOOL = "jellyfin_get_user_state_read_only"


class ArciMemberProvider(Protocol):
    def verify(self, stable_member_id: str) -> ArciMemberVerification: ...


class ArciEligibilityMCPServer:
    def __init__(self, provider: ArciMemberProvider | None = None) -> None:
        self.provider = provider or CurrentArciMcpMemberSource()

    def list_tools(self) -> list[dict[str, Any]]:
        return [_tool(ARCI_TOOL, "Verify one exact ARCI stable member ID; never fuzzy matches.", {
            "stable_member_id": {"type": "string", "minLength": 1, "maxLength": 240},
        }, ["stable_member_id"])]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if name != ARCI_TOOL or set(arguments) != {"stable_member_id"}:
            return _error("POLICY_DENIED")
        stable_id = str(arguments["stable_member_id"]).strip()
        if not stable_id or len(stable_id) > 240:
            return _error("INVALID_MEMBER_ID")
        try:
            result = self.provider.verify(stable_id)
        except Exception:
            result = ArciMemberVerification(
                outcome=VerificationOutcome.SOURCE_UNAVAILABLE,
                stable_member_id=stable_id,
                reason="arci_provider_failure",
            )
        return _result(result.model_dump(mode="json"), is_error=False)


class JellyfinReadOnlyClient:
    """Jellyfin 10.11 read-only user facade. Token is loaded, never returned/logged."""

    def __init__(self, base_url: str, api_token: str, *, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_token = api_token
        self.timeout = timeout

    @classmethod
    def from_config(cls, path: str | Path) -> "JellyfinReadOnlyClient":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(str(payload.get("jellyfin_url") or ""), str(payload.get("jellyfin_token") or ""))

    def list_users(self) -> tuple[JellyfinUserState, ...]:
        if not self.base_url or not self._api_token:
            raise RuntimeError("jellyfin_credentials_unavailable")
        request = urllib.request.Request(
            self.base_url + "/Users",
            headers={"Accept": "application/json", "X-Emby-Token": self._api_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            raise RuntimeError("jellyfin_source_unavailable") from exc
        if not isinstance(payload, list):
            raise RuntimeError("jellyfin_users_malformed")
        users = tuple(self._normalize_user(row) for row in payload)
        if len({row.user_id for row in users}) != len(users):
            raise RuntimeError("jellyfin_users_duplicate_id")
        return users

    def get_user(self, *, user_id: str | None = None, username: str | None = None) -> JellyfinUserState:
        if bool(user_id) == bool(username):
            raise ValueError("jellyfin_exact_identity_required")
        users = self.list_users()
        matches = [row for row in users if (
            row.user_id == user_id if user_id else row.username == username
        )]
        if len(matches) > 1:
            raise RuntimeError("jellyfin_user_ambiguous")
        return matches[0] if matches else JellyfinUserState(available=True)

    def _normalize_user(self, raw: object) -> JellyfinUserState:
        if not isinstance(raw, Mapping):
            raise RuntimeError("jellyfin_user_malformed")
        identity, name, policy = raw.get("Id"), raw.get("Name"), raw.get("Policy")
        if not isinstance(identity, str) or not identity or not isinstance(name, str) or not isinstance(policy, Mapping):
            raise RuntimeError("jellyfin_user_malformed")
        policy_safe = {
            "IsDisabled": bool(policy.get("IsDisabled", False)),
            "IsAdministrator": bool(policy.get("IsAdministrator", False)),
            "EnableMediaPlayback": bool(policy.get("EnableMediaPlayback", False)),
            "EnableAllFolders": bool(policy.get("EnableAllFolders", False)),
            "EnabledFolders": sorted(str(value) for value in (policy.get("EnabledFolders") or [])),
        }
        fingerprint = hashlib.sha256(
            json.dumps(policy_safe, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        content_hash = hashlib.sha256(f"{identity}|{name}|{fingerprint}".encode()).hexdigest()
        observed = datetime.now(timezone.utc)
        return JellyfinUserState(
            available=True, user_id=identity, username=name,
            enabled=not policy_safe["IsDisabled"], policy_fingerprint=fingerprint,
            evidence=(SourceEvidence(
                source_type="jellyfin_api", source_id=identity,
                locator="GET /Users", observed_at=observed, content_hash=content_hash,
            ),),
        )


class JellyfinUserMCPServer:
    def __init__(self, client: JellyfinReadOnlyClient) -> None:
        self.client = client

    def list_tools(self) -> list[dict[str, Any]]:
        return [
            _tool(JELLYFIN_LIST_TOOL, "List PII-minimized Jellyfin user state.", {}, []),
            _tool(JELLYFIN_GET_TOOL, "Get one exact Jellyfin user by native ID or exact username.", {
                "user_id": {"type": "string", "minLength": 1, "maxLength": 240},
                "username": {"type": "string", "minLength": 1, "maxLength": 240},
            }, []),
        ]

    def call(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if name == JELLYFIN_LIST_TOOL and not arguments:
                users = self.client.list_users()
                return _result({"ok": True, "users": [row.model_dump(mode="json") for row in users]})
            if name == JELLYFIN_GET_TOOL and set(arguments) <= {"user_id", "username"}:
                state = self.client.get_user(
                    user_id=str(arguments.get("user_id") or "") or None,
                    username=str(arguments.get("username") or "") or None,
                )
                return _result({"ok": True, "state": state.model_dump(mode="json")})
        except Exception as exc:
            code = str(exc) if str(exc).startswith("jellyfin_") else "SOURCE_UNAVAILABLE"
            return _error(code.upper())
        return _error("POLICY_DENIED")


def serve(server: ArciEligibilityMCPServer | JellyfinUserMCPServer) -> int:
    import sys
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            if method == "notifications/initialized":
                continue
            if method == "initialize":
                result = {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "bottazzi-service-identity", "version": "1"}}
            elif method == "tools/list":
                result = {"tools": server.list_tools()}
            elif method == "tools/call":
                params = request.get("params") or {}
                result = server.call(str(params.get("name") or ""), params.get("arguments") or {})
            else:
                raise ValueError("method_not_found")
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception:
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32603, "message": "internal_error"}}
        sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()
    return 0


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"name": name, "description": description, "inputSchema": {
        "type": "object", "properties": properties, "required": required,
        "additionalProperties": False,
    }}


def _result(payload: Mapping[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": dict(payload), "isError": is_error,
    }


def _error(code: str) -> dict[str, Any]:
    return _result({"ok": False, "status": code}, is_error=True)


__all__ = [
    "ARCI_TOOL", "JELLYFIN_GET_TOOL", "JELLYFIN_LIST_TOOL",
    "ArciEligibilityMCPServer", "JellyfinReadOnlyClient", "JellyfinUserMCPServer", "serve",
]
