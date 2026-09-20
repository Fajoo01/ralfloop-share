from __future__ import annotations

from scripts.ralf_md_goodify_api_server import ApiApplication


class FakeFlow:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def process_qr(self, qr_code):
        self.calls.append(qr_code)
        return dict(self.result)


def test_api_accepts_only_qr_code():
    flow = FakeFlow({"ok": True, "status": "DONATED_TO_TIREMM"})
    app = ApiApplication(flow)
    code, result = app.process({"qr_code": "QR-ABC"})
    assert code == 200
    assert result["status"] == "DONATED_TO_TIREMM"
    assert flow.calls == ["QR-ABC"]

    code, result = app.process({"qr_code": "QR-ABC", "recipient": "Other"})
    assert code == 400
    assert result["status"] == "INVALID_REQUEST"
    assert flow.calls == ["QR-ABC"]


def test_api_maps_processing_and_ambiguous_states():
    processing = ApiApplication(FakeFlow({"ok": True, "status": "PROCESSING"}))
    assert processing.process({"qr_code": "QR"})[0] == 202

    ambiguous = ApiApplication(FakeFlow({"ok": False, "status": "AMBIGUOUS_PURCHASE"}))
    assert ambiguous.process({"qr_code": "QR"})[0] == 409


def test_health_declares_fixed_recipient():
    health = ApiApplication(FakeFlow({})).health()
    assert health == {"ok": True, "service": "md-goodify", "recipient": "TIREMM INNANZ APS"}
