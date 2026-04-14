from pathlib import Path


def test_backend_source_contains_probe_stream_endpoint() -> None:
    text = Path("openshell_backend/app.py").read_text(encoding="utf-8")
    assert '"/sandboxes/{sid}/probe_stream"' in text


def test_real_adapter_source_contains_probe_stream_method() -> None:
    text = Path("ralfloop_agent/adapters/openshell_real_adapter.py").read_text(encoding="utf-8")
    assert "def probe_stream(" in text
