from pathlib import Path


def test_agent_audit_store_is_outside_immutable_release() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "openshell_backend/app.py"
    ).read_text(encoding="utf-8")
    assert 'AuditLogger(store_path="./logs")' not in source
    assert 'AuditLogger(store_path=str(BASE_DIR / "agent-audit"))' in source
