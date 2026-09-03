from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from ralfloop_agent.unified_assistant.service_identity import (
    ArciMemberVerification, JellyfinUserState, SourceEvidence, VerificationOutcome,
)
from ralfloop_agent.unified_assistant.service_identity_mcp import (
    ARCI_TOOL, JELLYFIN_GET_TOOL, JELLYFIN_LIST_TOOL,
    ArciEligibilityMCPServer, JellyfinUserMCPServer,
    JellyfinReadOnlyClient,
)


NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


class ArciFixture:
    def verify(self, stable_member_id):
        source = SourceEvidence(
            source_type="arci_mcp", source_id=stable_member_id,
            locator="fixture", observed_at=NOW,
            content_hash=hashlib.sha256(stable_member_id.encode()).hexdigest(),
        )
        return ArciMemberVerification(
            outcome=VerificationOutcome.VERIFIED_ELIGIBLE,
            stable_member_id=stable_member_id, card_status="VALID",
            evidence=(source,), reason="fixture",
        )


class JellyfinFixture:
    def list_users(self):
        return (JellyfinUserState(available=True, user_id="jf-1", username="fixture", enabled=True),)

    def get_user(self, *, user_id=None, username=None):
        return self.list_users()[0] if user_id == "jf-1" or username == "fixture" else JellyfinUserState(available=True)


def test_arci_mcp_is_exact_and_rejects_extra_inputs():
    server = ArciEligibilityMCPServer(ArciFixture())
    assert server.call(ARCI_TOOL, {"stable_member_id": "arci-1"})["structuredContent"]["outcome"] == "VERIFIED_ELIGIBLE"
    assert server.call(ARCI_TOOL, {"stable_member_id": "arci-1", "name": "fuzzy"})["isError"]
    schema = server.list_tools()[0]["inputSchema"]
    assert schema["additionalProperties"] is False


def test_jellyfin_mcp_is_read_only_and_has_no_delete_or_password_tool():
    server = JellyfinUserMCPServer(JellyfinFixture())
    names = {row["name"] for row in server.list_tools()}
    assert {JELLYFIN_LIST_TOOL, JELLYFIN_GET_TOOL} <= names
    assert not any("delete" in name or "password" in name or "execute" in name for name in names)
    assert server.call(JELLYFIN_LIST_TOOL, {})["structuredContent"]["users"][0]["user_id"] == "jf-1"
    assert server.call(JELLYFIN_GET_TOOL, {"username": "fixture"})["structuredContent"]["state"]["user_id"] == "jf-1"


def test_malformed_mcp_requests_fail_closed():
    server = JellyfinUserMCPServer(JellyfinFixture())
    assert server.call(JELLYFIN_GET_TOOL, {"username": "a", "unexpected": "x"})["isError"]
    assert server.call("jellyfin_delete_user", {"user_id": "jf-1"})["isError"]


def test_malformed_jellyfin_response_fails_closed():
    client = JellyfinReadOnlyClient("http://fixture.invalid", "fixture-secret")
    try:
        client._normalize_user({"Id": "jf-1", "Name": "fixture"})
    except RuntimeError as exc:
        assert str(exc) == "jellyfin_user_malformed"
    else:
        raise AssertionError("malformed Jellyfin response accepted")
