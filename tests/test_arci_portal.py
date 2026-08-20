from __future__ import annotations

from ralfloop_agent.unified_assistant.arci_portal import (
    ARCI_HOST,
    ARCI_MEMBERS_PATH,
    ARCI_OPERATION,
    ARCI_ORGANIZATION_PROFILE_FUNCTION,
    ArciPortalReadOnly,
)
from ralfloop_agent.unified_assistant.browser_read_only import FixedScriptResult
from ralfloop_agent.unified_assistant.browser_read_only import (
    BrowserPage,
    BrowserReadOnlyError,
    CdpReadOnlySnapshotClient,
)
import pytest


FOUND = {
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
}


class FakeReader:
    def __init__(self, value):
        self.value = value
        self.calls = []

    def call_fixed_function(self, **kwargs):
        self.calls.append(kwargs)
        return FixedScriptResult(
            value=self.value,
            operation=kwargs["operation"],
        )


def test_arci_provider_uses_fixed_exact_scope_and_returns_age_buckets():
    reader = FakeReader(dict(FOUND))

    result = ArciPortalReadOnly(reader).read_organization_profile()

    assert result.status == "FOUND"
    assert result.organization_name == "TIREMM INNANZ APS"
    assert result.member_count == 310
    assert (
        result.governance_under_15,
        result.governance_age_15_30,
        result.governance_over_30,
    ) == (0, 0, 8)
    assert result.write_operations == 0
    assert result.side_effects == 0
    assert reader.calls == [{
        "expected_host": ARCI_HOST,
        "expected_path_prefix": ARCI_MEMBERS_PATH,
        "expected_path": ARCI_MEMBERS_PATH,
        "function": ARCI_ORGANIZATION_PROFILE_FUNCTION,
        "arguments": (),
        "operation": ARCI_OPERATION,
    }]


def test_arci_provider_drops_member_pii_at_cdp_boundary():
    raw = {
        **FOUND,
        "members": [{
            "full_name": "Private Person",
            "email": "private@example.invalid",
            "tax_code": "PRIVATE",
            "birth_date": "1980-01-01",
        }],
    }

    result = ArciPortalReadOnly(FakeReader(raw)).read_organization_profile()
    serialized = result.model_dump(mode="json")

    assert result.status == "FOUND"
    assert "members" not in serialized
    assert "email" not in serialized
    assert "tax_code" not in serialized
    assert "birth_date" not in serialized


def test_arci_provider_fails_closed_on_inconsistent_aggregate():
    raw = {**FOUND, "governance_over_30": 7}

    result = ArciPortalReadOnly(FakeReader(raw)).read_organization_profile()

    assert result.status == "UNAVAILABLE"
    assert result.session_authenticated is False
    assert result.side_effects == 0


def test_arci_fixed_function_is_bounded_read_only_and_pii_minimized():
    script = ARCI_ORGANIZATION_PROFILE_FUNCTION

    assert "location.origin !== expectedOrigin" in script
    assert "location.pathname !== expectedPath" in script
    assert "organismidirigenti" in script
    assert "credentials: 'same-origin'" in script
    assert "governance_age_15_30" in script
    assert ".click(" not in script
    assert ".submit(" not in script
    assert "Page.navigate" not in script
    assert "email" not in script.casefold()
    assert "codicefiscale" not in script.casefold()


def test_cdp_page_scope_rejects_lookalike_origin_and_wrong_path():
    client = CdpReadOnlySnapshotClient("http://127.0.0.1:9236")
    client.pages = lambda: (
        BrowserPage(
            "lookalike",
            "x",
            "https://portale.arci.it.evil.invalid/admin/office/circolosoci/",
            "ws://127.0.0.1/lookalike",
        ),
        BrowserPage(
            "port",
            "x",
            "https://portale.arci.it:444/admin/office/circolosoci/",
            "ws://127.0.0.1/port",
        ),
        BrowserPage(
            "wrong-path",
            "x",
            "https://portale.arci.it/public/",
            "ws://127.0.0.1/path",
        ),
    )

    with pytest.raises(BrowserReadOnlyError, match="browser_page_unresolved"):
        client._page(
            ARCI_HOST,
            ARCI_MEMBERS_PATH,
            exact_path=ARCI_MEMBERS_PATH,
        )


def test_cdp_exact_path_ignores_arci_subpage():
    client = CdpReadOnlySnapshotClient("http://127.0.0.1:9236")
    exact = BrowserPage(
        "exact",
        "members",
        "https://portale.arci.it/admin/office/circolosoci/?q=x",
        "ws://127.0.0.1/exact",
    )
    client.pages = lambda: (
        exact,
        BrowserPage(
            "add",
            "add",
            "https://portale.arci.it/admin/office/circolosoci/add/",
            "ws://127.0.0.1/add",
        ),
    )

    assert client._page(
        ARCI_HOST,
        ARCI_MEMBERS_PATH,
        exact_path=ARCI_MEMBERS_PATH,
    ) == exact
