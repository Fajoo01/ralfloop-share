from fastapi.testclient import TestClient

from src.api import app

client = TestClient(app)


def test_domain_api_resolve_and_answer_deterministic():
    res = client.post("/lab/domains/resolve", json={"goal": "Quanto fa 2 + 3?"})
    assert res.status_code == 200
    assert res.json()["status"] == "resolved"
    ans = client.post("/lab/domains/answer", json={"goal": "Quanto fa 2 + 3?", "domain": "arithmetic_basic"})
    assert ans.status_code == 200
    assert ans.json()["answer"] == "5"


def test_domain_api_missing_returns_202():
    res = client.post("/lab/domains/answer", json={"goal": "Valuta il rischio di un dominio mai registrato"})
    assert res.status_code == 202
    assert res.json()["detail"]["status"] == "domain_creation_required"


def test_domain_api_jury_disabled_503(monkeypatch):
    monkeypatch.delenv("RALFLOOP_ENABLE_RECURSIVE_MAS_NATIVE", raising=False)
    res = client.post("/lab/domains/answer", json={"goal": "Qual è la strategia più prudente per questo incidente?", "domain": "incident_triage"})
    assert res.status_code == 503
    body = res.json()["detail"]
    assert body["jury_required"] is True
    assert body["jury_status"] == "disabled"


def test_domain_api_rejects_arbitrary_extra_path():
    res = client.post("/lab/domains/answer", json={"goal": "x", "domain": "arithmetic_basic", "python_path": "/bad"})
    assert res.status_code == 422


def test_domain_api_side_effect_requires_confirmation():
    res = client.post("/lab/domains/answer", json={"goal": "Invia telegram per incidente", "domain": "incident_triage"})
    assert res.status_code == 423
    assert res.json()["detail"]["external_action_executed"] is False
