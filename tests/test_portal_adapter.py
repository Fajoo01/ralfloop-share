from src.portal_adapter import ApprovalPortalService, PortalAdapter


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        self.calls.append("snapshot")
        return {"status": "OK"}

    def preview(self, operations):
        self.calls.append(("preview", operations))
        return {"status": "PREVIEW"}

    def request(self, operations, requested_by="ralf"):
        self.calls.append(("request", operations, requested_by))
        return {"status": "PENDING"}

    def execute(self, request_id):
        self.calls.append(("execute", request_id))
        return {"status": "EXECUTED"}


def test_approval_portal_service_wraps_methods():
    adapter = FakeAdapter()
    service = ApprovalPortalService(adapter, audit_path="/tmp/test_portal_audit.jsonl")

    assert service.snapshot()["status"] == "OK"
    assert service.preview([{"kind": "fill"}])["status"] == "PREVIEW"
    assert service.request([{"kind": "fill"}], requested_by="ralf")["status"] == "PENDING"
    assert service.execute("apr_12345678")["status"] == "EXECUTED"

    assert adapter.calls[0] == "snapshot"
    assert adapter.calls[1] == ("preview", [{"kind": "fill"}])
    assert adapter.calls[2] == ("request", [{"kind": "fill"}], "ralf")
    assert adapter.calls[3] == ("execute", "apr_12345678")
