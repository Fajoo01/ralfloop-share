from __future__ import annotations

from openshell_backend import app as backend


PROFILE = {
    "ok": True,
    "operation": "read_organization_profile",
    "status": "FOUND",
    "session_authenticated": True,
    "organization_name": "TIREMM INNANZ APS",
    "member_count": 310,
    "as_of": "2026-08-20",
    "governance_total": 8,
    "governance_dated": 8,
    "governance_under_15": 0,
    "governance_age_15_30": 0,
    "governance_over_30": 8,
    "provenance": ["arci_portal:organization"],
    "read_operations": ["arci.organization_profile.read"],
    "write_operations": 0,
    "side_effects": 0,
    "content_role": "untrusted_external_data",
    "writes": 0,
    "sends": 0,
}


class FakeGateway:
    def __init__(self) -> None:
        self.calls = 0

    def read_organization_profile(self):
        self.calls += 1
        return PROFILE


class FakeContext:
    def __init__(self, gateway: FakeGateway) -> None:
        self.gateway = gateway
        self.entered = False
        self.exited = False

    def __enter__(self):
        self.entered = True
        return self.gateway

    def __exit__(self, exc_type, exc, tb):
        self.exited = True


def test_backend_registers_portal_routes():
    paths = {route.path for route in backend.app.routes}

    assert "/portals/arci/profile" in paths
    assert "/portals/support4youth/snapshot" in paths
    assert "/portals/support4youth/preview" in paths
    assert "/portals/support4youth/requests" in paths
    assert "/portals/support4youth/requests/{request_id}/apply" in paths


def test_arci_endpoint_uses_fixed_read_only_gateway():
    gateway = FakeGateway()
    context = FakeContext(gateway)

    result = backend._read_arci_profile(lambda: context)

    assert result == PROFILE
    assert gateway.calls == 1
    assert context.entered is True
    assert context.exited is True
    assert result["writes"] == 0
    assert result["sends"] == 0


def test_arci_endpoint_fails_closed_without_error_details():
    secret = "socket=/private/path/mcp.sock token=secret"

    def broken_context():
        raise RuntimeError(secret)

    result = backend._read_arci_profile(broken_context)

    assert result == {
        "ok": False,
        "operation": "read_organization_profile",
        "status": "SOURCE_UNAVAILABLE",
        "session_authenticated": False,
        "read_operations": [],
        "write_operations": 0,
        "side_effects": 0,
        "writes": 0,
        "sends": 0,
    }
    assert secret not in repr(result)
