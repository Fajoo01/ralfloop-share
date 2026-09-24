from pathlib import Path


def test_memory_rag_overlay_does_not_shadow_unified_session_store() -> None:
    overlay = Path("deploy/systemd/ralfloop-backend-memory-rag-overlay.conf").read_text(
        encoding="utf-8"
    )

    assert "/ralfloop_agent/cli/session_store.py:" not in overlay
    assert "Unified Assistant and runtime config are canonical" in overlay
