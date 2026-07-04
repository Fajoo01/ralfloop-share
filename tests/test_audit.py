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
