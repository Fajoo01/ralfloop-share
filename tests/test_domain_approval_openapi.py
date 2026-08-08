import importlib
import json
import sys


def test_domain_approval_routes_build_openapi(monkeypatch, tmp_path):
    monkeypatch.setenv("RALFLOOP_ENABLE_TELEGRAM_APPROVAL_GATE", "1")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_DB", str(tmp_path / "approval.sqlite"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_AUDIT_LOG", str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_ALLOWED_USER_IDS", "111")
    monkeypatch.setenv("RALFLOOP_TELEGRAM_APPROVAL_HMAC_KEY_FILE", str(tmp_path / "hmac.key"))
    (tmp_path / "hmac.key").write_text("secret", encoding="utf-8")

    sys.modules.pop("openshell_backend.app", None)
    module = importlib.import_module("openshell_backend.app")
    schema = module.app.openapi()

    paths = set(schema.get("paths", {}))
    required = {
        "/domain-approvals/requests",
        "/domain-approvals/{request_id}",
        "/domain-approvals/{request_id}/decision",
        "/domain-approvals/{request_id}/cancel",
    }
    assert required <= paths

    decision_route = next(
        route
        for route in module.app.routes
        if getattr(route, "path", "") == "/domain-approvals/{request_id}/decision"
    )
    assert "StarletteRequest" in decision_route.endpoint.__globals__

    decision_schema = schema["paths"]["/domain-approvals/{request_id}/decision"]["post"]
    params = json.dumps(decision_schema.get("parameters", []), sort_keys=True)
    body = json.dumps(decision_schema.get("requestBody", {}), sort_keys=True)
    rendered = json.dumps(decision_schema, sort_keys=True)
    assert '"name": "request"' not in params
    assert "ForwardRef" not in rendered
    assert "StarletteRequest" not in body
