from pathlib import Path


def test_backend_audit_log_location_is_ignored_runtime_data() -> None:
    audit_path = Path("/home/sibilla-cumana/ralfloop_agent_scaffold/.openshell_backend/audit.jsonl")
    assert audit_path.parent.name == ".openshell_backend"
