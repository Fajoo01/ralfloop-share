from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
import urllib.error
import urllib.request

from .jellyfin_semantic import (
    JellyfinHealth, JellyfinItem, JellyfinLibrary, JellyfinMediaStream,
    JellyfinProposalAction, JellyfinProposalService, evidence_for_json,
)

from .service_identity import (
    ArciMemberVerification, CurrentArciMcpMemberSource, JellyfinUserState,
    SourceEvidence, VerificationOutcome,
)


MCP_PROTOCOL_VERSION = "2024-11-05"
ARCI_TOOL = "arci_verify_member_eligibility"
JELLYFIN_LIST_TOOL = "jellyfin_list_users_read_only"
JELLYFIN_GET_TOOL = "jellyfin_get_user_state_read_only"
JELLYFIN_READ_TOOLS = {
    "jellyfin_list_users", "jellyfin_get_user", "jellyfin_get_user_policy",
    "jellyfin_get_health", "jellyfin_list_libraries", "jellyfin_get_item",
    "jellyfin_get_media_streams",
}
JELLYFIN_PROPOSE_TOOLS = {
    "jellyfin_propose_user_create": JellyfinProposalAction.USER_CREATE,
    "jellyfin_propose_user_link": JellyfinProposalAction.USER_LINK,
    "jellyfin_propose_user_enable": JellyfinProposalAction.USER_ENABLE,
    "jellyfin_propose_user_disable": JellyfinProposalAction.USER_DISABLE,
}


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
        payload = self._get_json("/Users")
        if not isinstance(payload, list):
            raise RuntimeError("jellyfin_users_malformed")
        users = tuple(self._normalize_user(row) for row in payload)
        if len({row.user_id for row in users}) != len(users):
            raise RuntimeError("jellyfin_users_duplicate_id")
        return users

    def get_user(self, *, user_id: str | None = None, username: str | None = None) -> JellyfinUserState:
        if bool(user_id) == bool(username):
            raise ValueError("jellyfin_exact_identity_required")
        if user_id:
            _safe_id(user_id)
            raw = self._get_json(f"/Users/{user_id}")
            state = self._normalize_user(raw)
            if state.user_id != user_id:
                raise RuntimeError("jellyfin_user_identity_mismatch")
            return state
        users = self.list_users()
        matches = [row for row in users if (
            row.user_id == user_id if user_id else row.username == username
        )]
        if len(matches) > 1:
            raise RuntimeError("jellyfin_user_ambiguous")
        return matches[0] if matches else JellyfinUserState(available=True)

    def get_user_policy(self, user_id: str) -> Mapping[str, Any]:
        state = self.get_user(user_id=user_id)
        if not state.user_id:
            raise RuntimeError("jellyfin_user_not_found")
        return {
            "user_id": state.user_id, "enabled": state.enabled,
            "policy_fingerprint": state.policy_fingerprint,
            "evidence": [row.model_dump(mode="json") for row in state.evidence],
        }

    def get_health(self) -> JellyfinHealth:
        raw = self._get_json("/System/Info")
        if not isinstance(raw, Mapping):
            raise RuntimeError("jellyfin_health_malformed")
        server_id = raw.get("Id")
        evidence = evidence_for_json(str(server_id or "server"), "GET /System/Info", raw)
        return JellyfinHealth(
            available=True,
            server_id=str(server_id) if server_id else None,
            server_name=str(raw["ServerName"]) if raw.get("ServerName") else None,
            version=str(raw["Version"]) if raw.get("Version") else None,
            startup_wizard_completed=raw.get("StartupWizardCompleted") if isinstance(raw.get("StartupWizardCompleted"), bool) else None,
            evidence=(evidence,),
        )

    def list_libraries(self) -> tuple[JellyfinLibrary, ...]:
        raw = self._get_json("/Library/VirtualFolders")
        if not isinstance(raw, list):
            raise RuntimeError("jellyfin_libraries_malformed")
        rows = []
        for item in raw:
            if not isinstance(item, Mapping) or not item.get("ItemId") or not item.get("Name"):
                raise RuntimeError("jellyfin_library_malformed")
            rows.append(JellyfinLibrary(
                item_id=str(item["ItemId"]), name=str(item["Name"]),
                collection_type=str(item["CollectionType"]) if item.get("CollectionType") else None,
            ))
        return tuple(rows)

    def get_item(self, item_id: str, *, user_id: str) -> JellyfinItem:
        _safe_id(item_id); _safe_id(user_id)
        raw = self._get_json(f"/Users/{user_id}/Items/{item_id}")
        if not isinstance(raw, Mapping) or str(raw.get("Id") or "") != item_id or not raw.get("Name") or not raw.get("Type"):
            raise RuntimeError("jellyfin_item_malformed")
        return JellyfinItem(
            item_id=item_id, name=str(raw["Name"]), item_type=str(raw["Type"]),
            series_name=str(raw["SeriesName"]) if raw.get("SeriesName") else None,
            season_name=str(raw["SeasonName"]) if raw.get("SeasonName") else None,
            index_number=raw.get("IndexNumber") if isinstance(raw.get("IndexNumber"), int) else None,
            parent_index_number=raw.get("ParentIndexNumber") if isinstance(raw.get("ParentIndexNumber"), int) else None,
            evidence=(evidence_for_json(item_id, f"GET /Users/{user_id}/Items/{item_id}", raw),),
        )

    def get_media_streams(self, item_id: str, *, user_id: str) -> tuple[JellyfinMediaStream, ...]:
        _safe_id(item_id); _safe_id(user_id)
        raw = self._get_json(f"/Items/{item_id}/PlaybackInfo?UserId={user_id}")
        if not isinstance(raw, Mapping) or not isinstance(raw.get("MediaSources"), list):
            raise RuntimeError("jellyfin_media_sources_malformed")
        output = []
        for source in raw["MediaSources"]:
            if not isinstance(source, Mapping) or not source.get("Id") or not isinstance(source.get("MediaStreams"), list):
                raise RuntimeError("jellyfin_media_source_malformed")
            for stream in source["MediaStreams"]:
                if not isinstance(stream, Mapping) or not isinstance(stream.get("Index"), int) or not stream.get("Type"):
                    raise RuntimeError("jellyfin_media_stream_malformed")
                output.append(JellyfinMediaStream(
                    media_source_id=str(source["Id"]), index=stream["Index"], type=str(stream["Type"]),
                    codec=str(stream["Codec"]) if stream.get("Codec") else None,
                    language=str(stream["Language"]) if stream.get("Language") else None,
                    display_title=str(stream["DisplayTitle"]) if stream.get("DisplayTitle") else None,
                    is_default=stream.get("IsDefault") if isinstance(stream.get("IsDefault"), bool) else None,
                    is_external=stream.get("IsExternal") if isinstance(stream.get("IsExternal"), bool) else None,
                    channels=stream.get("Channels") if isinstance(stream.get("Channels"), int) else None,
                    width=stream.get("Width") if isinstance(stream.get("Width"), int) else None,
                    height=stream.get("Height") if isinstance(stream.get("Height"), int) else None,
                    bitrate=stream.get("BitRate") if isinstance(stream.get("BitRate"), int) else None,
                ))
        return tuple(output)

    def _get_json(self, path: str) -> Any:
        if not self.base_url or not self._api_token:
            raise RuntimeError("jellyfin_credentials_unavailable")
        request = urllib.request.Request(
            self.base_url + path,
            headers={"Accept": "application/json", "X-Emby-Token": self._api_token},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except (OSError, ValueError, urllib.error.HTTPError) as exc:
            raise RuntimeError("jellyfin_source_unavailable") from exc

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
    def __init__(self, client: JellyfinReadOnlyClient, proposals: JellyfinProposalService | None = None) -> None:
        self.client = client
        self.proposals = proposals or JellyfinProposalService()

    def list_tools(self) -> list[dict[str, Any]]:
        tools = [
            _tool(JELLYFIN_LIST_TOOL, "List PII-minimized Jellyfin user state.", {}, []),
            _tool(JELLYFIN_GET_TOOL, "Get one exact Jellyfin user by native ID or exact username.", {
                "user_id": {"type": "string", "minLength": 1, "maxLength": 240},
                "username": {"type": "string", "minLength": 1, "maxLength": 240},
            }, []),
        ]
        tools.extend([
            _tool("jellyfin_list_users", "List PII-minimized Jellyfin users.", {}, []),
            _tool("jellyfin_get_user", "Get one exact Jellyfin user.", {"user_id": _id_schema()}, ["user_id"]),
            _tool("jellyfin_get_user_policy", "Read one exact user policy fingerprint.", {"user_id": _id_schema()}, ["user_id"]),
            _tool("jellyfin_get_health", "Read Jellyfin server health.", {}, []),
            _tool("jellyfin_list_libraries", "List Jellyfin libraries without filesystem paths.", {}, []),
            _tool("jellyfin_get_item", "Read one exact Jellyfin item.", {"item_id": _id_schema(), "user_id": _id_schema()}, ["item_id", "user_id"]),
            _tool("jellyfin_get_media_streams", "Read bounded media stream metadata.", {"item_id": _id_schema(), "user_id": _id_schema()}, ["item_id", "user_id"]),
        ])
        proposal_properties = {
            "arci_member_id": _id_schema(), "jellyfin_user_id": _id_schema(),
            "username": {"type": "string", "minLength": 1, "maxLength": 240},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
        }
        for name, action in JELLYFIN_PROPOSE_TOOLS.items():
            required = ["arci_member_id", "reason"]
            required.append("username" if action is JellyfinProposalAction.USER_CREATE else "jellyfin_user_id")
            tools.append(_tool(name, "Prepare non-executable approval-controlled Jellyfin proposal.", proposal_properties, required))
        return tools

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
            if name == "jellyfin_list_users" and not arguments:
                return _result({"ok": True, "users": [row.model_dump(mode="json") for row in self.client.list_users()]})
            if name == "jellyfin_get_user" and set(arguments) == {"user_id"}:
                return _result({"ok": True, "state": self.client.get_user(user_id=str(arguments["user_id"])).model_dump(mode="json")})
            if name == "jellyfin_get_user_policy" and set(arguments) == {"user_id"}:
                return _result({"ok": True, "policy": self.client.get_user_policy(str(arguments["user_id"]))})
            if name == "jellyfin_get_health" and not arguments:
                return _result({"ok": True, "health": self.client.get_health().model_dump(mode="json")})
            if name == "jellyfin_list_libraries" and not arguments:
                return _result({"ok": True, "libraries": [row.model_dump(mode="json") for row in self.client.list_libraries()]})
            if name == "jellyfin_get_item" and set(arguments) == {"item_id", "user_id"}:
                item = self.client.get_item(str(arguments["item_id"]), user_id=str(arguments["user_id"]))
                return _result({"ok": True, "item": item.model_dump(mode="json")})
            if name == "jellyfin_get_media_streams" and set(arguments) == {"item_id", "user_id"}:
                rows = self.client.get_media_streams(str(arguments["item_id"]), user_id=str(arguments["user_id"]))
                return _result({"ok": True, "streams": [row.model_dump(mode="json") for row in rows]})
            if name in JELLYFIN_PROPOSE_TOOLS and set(arguments) <= {"arci_member_id", "jellyfin_user_id", "username", "reason"}:
                proposal = self.proposals.prepare(
                    JELLYFIN_PROPOSE_TOOLS[name],
                    arci_member_id=str(arguments.get("arci_member_id") or ""),
                    jellyfin_user_id=str(arguments.get("jellyfin_user_id") or "") or None,
                    username=str(arguments.get("username") or "") or None,
                    reason=str(arguments.get("reason") or ""), evidence=(),
                )
                return _result({"ok": True, "proposal": proposal.model_dump(mode="json"), "writes": 0})
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


def _id_schema() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": 240, "pattern": r"^[A-Za-z0-9_.:-]+$"}


def _safe_id(value: str) -> str:
    import re
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,240}", value):
        raise ValueError("jellyfin_native_id_invalid")
    return value


def _result(payload: Mapping[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "structuredContent": dict(payload), "isError": is_error,
    }


def _error(code: str) -> dict[str, Any]:
    return _result({"ok": False, "status": code}, is_error=True)


__all__ = [
    "ARCI_TOOL", "JELLYFIN_GET_TOOL", "JELLYFIN_LIST_TOOL", "JELLYFIN_PROPOSE_TOOLS", "JELLYFIN_READ_TOOLS",
    "ArciEligibilityMCPServer", "JellyfinReadOnlyClient", "JellyfinUserMCPServer", "serve",
]
