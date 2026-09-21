from __future__ import annotations

from scripts.ralf_md_goodify_api_server import ApiApplication, authorized_header


class FakeFlow:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def process_qr(self, qr_code):
        self.calls.append(qr_code)
        return dict(self.result)


class FakeAuthenticator:
    def __init__(self, result=None, error=None):
        self.result = result or {"access_token_saved": True}
        self.error = error
        self.calls = []

    def login(self, email, password):
        self.calls.append((email, password))
        if self.error:
            raise self.error
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
    health = ApiApplication(FakeFlow({}), FakeAuthenticator()).health()
    assert health["ok"] is True
    assert health["service"] == "md-goodify"
    assert health["recipient"] == "TIREMM INNANZ APS"
    assert isinstance(health["enrolled"], bool)


def test_enroll_accepts_only_email_and_password():
    auth = FakeAuthenticator()
    app = ApiApplication(FakeFlow({}), auth)
    code, result = app.enroll({"email": "f@example.test", "password": "secret"})
    assert code == 200
    assert result == {"ok": True, "status": "ENROLLED"}
    assert auth.calls == [("f@example.test", "secret")]

    code, result = app.enroll({"email": "f@example.test", "password": "secret", "save": True})
    assert code == 400
    assert result["status"] == "INVALID_REQUEST"
    assert auth.calls == [("f@example.test", "secret")]


def test_enroll_rejects_bad_credentials_without_echoing_them():
    auth = FakeAuthenticator(error=ValueError("bad credentials"))
    code, result = ApiApplication(FakeFlow({}), auth).enroll({"email": "f@example.test", "password": "secret"})
    assert code == 401
    assert result == {"ok": False, "status": "LOGIN_REJECTED"}
    assert "secret" not in str(result)


def test_authorized_header_requires_exact_bearer_token():
    token = "a" * 64
    assert authorized_header(f"Bearer {token}", token) is True
    assert authorized_header(token, token) is False
    assert authorized_header("Bearer wrong", token) is False
    assert authorized_header("", token) is False


class FakeHistory:
    def __init__(self, rows):
        self.rows = rows

    def get_donations(self):
        return {"donations": list(self.rows)}


def test_stats_sums_md_history_and_derives_unit(monkeypatch):
    rows = [
        {"Goodify_donatedAmount": "1,00", "month": True},
        {"Goodify_donatedAmount": "1,00", "month": True},
        {"Goodify_donatedAmount": "", "month": False},
    ]
    monkeypatch.setattr("scripts.ralf_md_goodify_api_server._completed_tiremm_count", lambda: 2)
    monkeypatch.setattr("scripts.ralf_md_goodify_api_server._completed_tiremm_count_month", lambda _key: 1)
    monkeypatch.setattr("scripts.ralf_md_goodify_api_server._row_in_month", lambda row, _year, _month: bool(row.get("month")))
    app = ApiApplication(FakeFlow({}), FakeAuthenticator(), FakeHistory(rows))
    code, result = app.stats()
    assert code == 200
    assert result["md_donations_count"] == 3
    assert result["md_total_eur"] == "3.00"
    assert result["md_month_count"] == 2
    assert result["md_month_total_eur"] == "2.00"
    assert result["unit_donation_eur"] == "1.00"
    assert result["tiremm_completed_count"] == 2
    assert result["tiremm_total_eur"] == "2.00"
    assert result["tiremm_month_count"] == 1
    assert result["tiremm_month_total_eur"] == "1.00"
