from openshell_backend.contracts.coder_patch_apply import (
    _extract_code_block,
    _extract_target_symbol_block,
    _replace_top_level_symbol,
)


def test_extract_code_block_from_fenced_python() -> None:
    text = """```python
def _simple_local_grammar_fallback(phrase: str):
    return []
```"""
    out = _extract_code_block(text)
    assert out is not None
    assert "def _simple_local_grammar_fallback" in out


def test_extract_target_symbol_block() -> None:
    code = """
def x():
    pass

def _simple_local_grammar_fallback(phrase: str):
    return []

def y():
    pass
"""
    out = _extract_target_symbol_block(code, "_simple_local_grammar_fallback")
    assert out is not None
    assert out.startswith("def _simple_local_grammar_fallback")


def test_replace_top_level_symbol() -> None:
    src = """
def a():
    return 1

def _simple_local_grammar_fallback(phrase: str):
    return ["old"]

def b():
    return 2
"""
    new_block = """
def _simple_local_grammar_fallback(phrase: str):
    return ["new"]
"""
    out = _replace_top_level_symbol(src, "_simple_local_grammar_fallback", new_block)
    assert out is not None
    assert 'return ["new"]' in out
    assert 'return ["old"]' not in out
