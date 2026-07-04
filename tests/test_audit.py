import json

from src.audit import log_operation


def test_log_operation(tmp_path):
    audit_path = tmp_path / "audit.jsonl"

    written = log_operation("unit_test", {"route": {"mode": "check_only"}}, audit_path=audit_path)

    assert written == audit_path
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["operation"] == "unit_test"
    assert entry["route"]["mode"] == "check_only"
    assert entry["timestamp"]


def test_audit_is_append_only(tmp_path):
    audit_path = tmp_path / "audit.jsonl"

    log_operation("first", {"result_status": "ok"}, audit_path=audit_path)
    log_operation("second", {"result_status": "ok"}, audit_path=audit_path)

    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["operation"] == "first"
    assert json.loads(lines[1])["operation"] == "second"


def test_audit_redacts_secrets_and_full_body(tmp_path):
    audit_path = tmp_path / "audit.jsonl"

    log_operation(
        "redaction",
        {
            "token": "secret-token",
            "password": "secret-password",
            "body": "full email body",
            "body_preview": "short preview allowed",
            "nested": {"client_secret": "hidden"},
        },
        audit_path=audit_path,
    )

    text = audit_path.read_text(encoding="utf-8")
    assert "secret-token" not in text
    assert "secret-password" not in text
    assert "full email body" not in text
    assert "hidden" not in text
    assert "short preview allowed" in text
