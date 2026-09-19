from pathlib import Path


def test_agent_audit_store_is_outside_immutable_release() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "openshell_backend/app.py"
    ).read_text(encoding="utf-8")
    assert 'AuditLogger(store_path="./logs")' not in source
    assert 'AuditLogger(store_path=str(BASE_DIR / "agent-audit"))' in source
    assert 'RALF_OPEN_SHELL_STATE_DIR' in source


def test_agent_self_backend_url_is_configurable() -> None:
    source = (
        Path(__file__).resolve().parents[1] / "openshell_backend/app.py"
    ).read_text(encoding="utf-8")
    assert 'RALF_OPEN_SHELL_BASE_URL' in source
    assert 'OpenShellAdapterReal(base_url=SELF_BASE_URL)' in source
