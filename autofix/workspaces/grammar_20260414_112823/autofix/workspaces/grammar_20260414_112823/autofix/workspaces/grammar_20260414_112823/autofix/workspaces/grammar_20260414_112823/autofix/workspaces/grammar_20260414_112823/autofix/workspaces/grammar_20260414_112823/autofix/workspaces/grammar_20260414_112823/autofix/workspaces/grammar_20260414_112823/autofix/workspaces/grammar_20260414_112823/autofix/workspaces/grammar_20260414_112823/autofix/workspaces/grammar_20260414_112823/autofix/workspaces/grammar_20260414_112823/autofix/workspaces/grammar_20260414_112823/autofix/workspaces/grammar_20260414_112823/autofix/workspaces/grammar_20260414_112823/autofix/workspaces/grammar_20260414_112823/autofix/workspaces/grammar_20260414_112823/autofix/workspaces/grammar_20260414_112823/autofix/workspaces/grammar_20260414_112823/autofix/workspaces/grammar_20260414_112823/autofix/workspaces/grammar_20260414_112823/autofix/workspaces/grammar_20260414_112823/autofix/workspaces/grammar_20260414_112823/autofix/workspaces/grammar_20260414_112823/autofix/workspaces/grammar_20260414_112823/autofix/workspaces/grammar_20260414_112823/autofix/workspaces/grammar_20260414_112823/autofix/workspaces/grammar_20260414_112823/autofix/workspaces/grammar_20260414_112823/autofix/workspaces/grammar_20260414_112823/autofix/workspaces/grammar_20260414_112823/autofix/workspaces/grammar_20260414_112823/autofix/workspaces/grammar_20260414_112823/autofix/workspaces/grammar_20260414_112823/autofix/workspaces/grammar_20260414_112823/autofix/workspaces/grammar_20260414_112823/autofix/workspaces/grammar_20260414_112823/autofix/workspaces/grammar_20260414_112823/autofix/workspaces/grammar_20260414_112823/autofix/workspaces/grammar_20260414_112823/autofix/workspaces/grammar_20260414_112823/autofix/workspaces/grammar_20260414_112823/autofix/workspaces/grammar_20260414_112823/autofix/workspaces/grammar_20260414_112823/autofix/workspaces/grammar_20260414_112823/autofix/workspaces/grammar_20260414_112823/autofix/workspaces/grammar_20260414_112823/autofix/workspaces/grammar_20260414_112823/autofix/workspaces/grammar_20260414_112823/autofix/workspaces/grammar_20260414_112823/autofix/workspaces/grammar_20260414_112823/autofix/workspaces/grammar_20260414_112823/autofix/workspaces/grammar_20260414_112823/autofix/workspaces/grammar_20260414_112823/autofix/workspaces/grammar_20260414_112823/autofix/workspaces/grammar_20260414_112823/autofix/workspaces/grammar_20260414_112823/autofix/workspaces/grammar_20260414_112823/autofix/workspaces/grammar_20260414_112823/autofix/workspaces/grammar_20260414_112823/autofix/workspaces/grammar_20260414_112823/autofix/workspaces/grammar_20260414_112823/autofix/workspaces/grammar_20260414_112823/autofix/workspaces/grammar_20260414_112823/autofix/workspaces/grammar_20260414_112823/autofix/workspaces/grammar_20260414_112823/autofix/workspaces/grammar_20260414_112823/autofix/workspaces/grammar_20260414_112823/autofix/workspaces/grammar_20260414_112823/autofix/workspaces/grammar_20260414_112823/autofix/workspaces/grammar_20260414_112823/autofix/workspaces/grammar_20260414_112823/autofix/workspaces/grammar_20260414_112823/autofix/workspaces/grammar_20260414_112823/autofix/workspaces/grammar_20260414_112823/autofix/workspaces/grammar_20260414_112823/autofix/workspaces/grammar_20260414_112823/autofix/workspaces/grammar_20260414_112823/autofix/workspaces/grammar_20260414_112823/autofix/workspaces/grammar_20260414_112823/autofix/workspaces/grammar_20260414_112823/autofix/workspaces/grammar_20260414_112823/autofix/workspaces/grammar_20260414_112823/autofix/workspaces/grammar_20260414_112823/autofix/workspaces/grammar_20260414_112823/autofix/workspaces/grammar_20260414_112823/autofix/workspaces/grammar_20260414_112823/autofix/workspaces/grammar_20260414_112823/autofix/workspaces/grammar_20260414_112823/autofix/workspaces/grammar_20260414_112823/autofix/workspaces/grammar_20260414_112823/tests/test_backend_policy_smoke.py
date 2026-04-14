from pathlib import Path


def test_backend_runtime_dir_exists() -> None:
    runtime_dir = Path("/home/sibilla-cumana/ralfloop_agent_scaffold/.openshell_backend")
    assert runtime_dir.exists()
    assert runtime_dir.is_dir()
