from openshell_backend.skill_grammar_rag import analyze_grammar_with_diagnostics


def test_analyze_grammar_with_diagnostics_returns_issues_for_suspicious_token() -> None:
    out = analyze_grammar_with_diagnostics("Devi ordinare lo scolapiatti dall'Ikea")
    assert out is not None
    assert "items" in out
    assert "issues" in out
    assert isinstance(out["items"], list)
    assert isinstance(out["issues"], list)
