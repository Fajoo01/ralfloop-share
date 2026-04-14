from pathlib import Path


def test_openshell_backend_source_contains_sandbox_inspect_endpoints() -> None:
    app_path = Path("openshell_backend/app.py")
    text = app_path.read_text(encoding="utf-8")
    assert '@app.get("/sandboxes")' in text
    assert '@app.get("/sandboxes/{sid}")' in text
