from pathlib import Path

from openshell_backend.contracts.coder_patch_apply import apply_coder_patch_permanently


def test_apply_coder_patch_permanently_writes_target_file(tmp_path: Path) -> None:
    repo = tmp_path
    target = repo / "openshell_backend" / "skill_grammar_rag.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        """
def a():
    return 1

def _simple_local_grammar_fallback(phrase: str):
    return ["old"]

def b():
    return 2
""".lstrip(),
        encoding="utf-8",
    )

    af = {
        "coder_text": """```python
def _simple_local_grammar_fallback(phrase: str):
    return ["new"]
```""",
        "coder_patch_candidate": {
            "target_file": "openshell_backend/skill_grammar_rag.py",
            "target_symbol": "_simple_local_grammar_fallback",
        },
        "coder_validation_attempt": {
            "ok": True,
        },
    }

    out = apply_coder_patch_permanently(
        repo_root=str(repo),
        autofix_candidate=af,
    )

    assert out is not None
    assert out["ok"] is True
    assert out["stage"] == "applied"
    body = target.read_text(encoding="utf-8")
    assert 'return ["new"]' in body
    assert 'return ["old"]' not in body
