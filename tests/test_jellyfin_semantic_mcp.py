from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from ralfloop_agent.unified_assistant.jellyfin_semantic import (
    JellyfinHealth, JellyfinItem, JellyfinLibrary, JellyfinMediaStream,
)
from ralfloop_agent.unified_assistant.service_identity import JellyfinUserState, SourceEvidence
from ralfloop_agent.unified_assistant.service_identity_mcp import JellyfinUserMCPServer


NOW = datetime(2026, 9, 3, tzinfo=timezone.utc)


def evidence(identity="jf-1"):
    return SourceEvidence(
        source_type="jellyfin_api", source_id=identity, locator="fixture",
        observed_at=NOW, content_hash=hashlib.sha256(identity.encode()).hexdigest(),
    )


class JellyfinSemanticFixture:
    test_class = "SANITIZED_FIXTURE"

    def list_users(self):
        return (JellyfinUserState(available=True, user_id="jf-1", username="synthetic", enabled=True, evidence=(evidence(),)),)

    def get_user(self, *, user_id=None, username=None):
        return self.list_users()[0] if user_id == "jf-1" else JellyfinUserState(available=True)

    def get_user_policy(self, user_id):
        return {"user_id": user_id, "enabled": True, "policy_fingerprint": "abc", "evidence": []}

    def get_health(self):
        return JellyfinHealth(available=True, server_id="server-1", version="10.11", evidence=(evidence("server-1"),))

    def list_libraries(self):
        return (JellyfinLibrary(item_id="library-1", name="Synthetic Movies", collection_type="movies"),)

    def get_item(self, item_id, *, user_id):
        return JellyfinItem(item_id=item_id, name="Synthetic Item", item_type="Movie", evidence=(evidence(item_id),))

    def get_media_streams(self, item_id, *, user_id):
        return (JellyfinMediaStream(media_source_id="source-1", index=0, type="Audio", codec="aac", language="ita"),)


def test_jellyfin_complete_read_surface_is_semantic_and_strict():
    server = JellyfinUserMCPServer(JellyfinSemanticFixture())
    names = {row["name"] for row in server.list_tools()}
    required = {
        "jellyfin_list_users", "jellyfin_get_user", "jellyfin_get_user_policy",
        "jellyfin_get_health", "jellyfin_list_libraries", "jellyfin_get_item",
        "jellyfin_get_media_streams",
    }
    assert required <= names
    assert not any("raw" in name or "request" in name or "delete" in name for name in names)
    assert server.call("jellyfin_get_health", {})["structuredContent"]["health"]["available"] is True
    assert server.call("jellyfin_get_item", {"item_id": "item-1", "user_id": "jf-1"})["structuredContent"]["item"]["item_id"] == "item-1"
    assert server.call("jellyfin_get_media_streams", {"item_id": "item-1", "user_id": "jf-1"})["structuredContent"]["streams"][0]["language"] == "ita"
    assert server.call("jellyfin_get_item", {"item_id": "item-1", "user_id": "jf-1", "url": "x"})["isError"]


def test_jellyfin_proposals_are_deterministic_non_executable_and_no_delete():
    server = JellyfinUserMCPServer(JellyfinSemanticFixture())
    arguments = {"arci_member_id": "member-1", "username": "synthetic.user", "reason": "eligible"}
    first = server.call("jellyfin_propose_user_create", arguments)["structuredContent"]["proposal"]
    second = server.call("jellyfin_propose_user_create", arguments)["structuredContent"]["proposal"]

    assert first == second
    assert first["requires_approval"] is True
    assert first["executable"] is False
    assert server.call("jellyfin_delete_user", {"user_id": "jf-1"})["isError"]


def test_enable_disable_and_link_require_exact_native_user_id():
    server = JellyfinUserMCPServer(JellyfinSemanticFixture())
    for tool in (
        "jellyfin_propose_user_link", "jellyfin_propose_user_enable",
        "jellyfin_propose_user_disable",
    ):
        good = server.call(tool, {
            "arci_member_id": "member-1", "jellyfin_user_id": "jf-1", "reason": "shadow",
        })
        assert good["structuredContent"]["proposal"]["jellyfin_user_id"] == "jf-1"
        assert server.call(tool, {"arci_member_id": "member-1", "reason": "shadow"})["isError"]
