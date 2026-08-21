from src.arci_portal_adapter import ArciPortalAdapter


class FakeArciGateway:
    def __init__(self, profile):
        self.profile = profile

    def read_organization_profile(self):
        return self.profile


def test_arci_login_status_reports_authenticated():
    gateway = FakeArciGateway({
        "status": "FOUND",
        "session_authenticated": True,
        "organization_name": "TIREMM INNANZ APS",
        "member_count": 310,
    })

    result = ArciPortalAdapter(gateway).login_status()

    assert result["status"] == "OK"
    assert result["authenticated"] is True
    assert result["organization_name"] == "TIREMM INNANZ APS"
    assert result["member_count"] == 310


def test_arci_write_operations_are_rejected_read_only():
    gateway = FakeArciGateway({})
    adapter = ArciPortalAdapter(gateway)

    preview = adapter.preview([{"kind": "fill"}])
    request = adapter.request([{"kind": "fill"}])
    execute = adapter.execute("apr_12345678")

    assert preview["status"] == "READ_ONLY_PORTAL"
    assert request["status"] == "REJECTED_READ_ONLY"
    assert execute["status"] == "REJECTED_READ_ONLY"
