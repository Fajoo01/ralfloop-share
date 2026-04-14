from openshell_backend.skill_grammar_rag import analyze_grammar_with_diagnostics


def test_analyze_grammar_with_diagnostics_exposes_raw_and_final() -> None:
    out = analyze_grammar_with_diagnostics("Devi ordinare lo scolapiatti dall'Ikea")
    assert out is not None

    assert "raw_items" in out
    assert "raw_issues" in out
    assert "final_items" in out
    assert "final_issues" in out

    assert isinstance(out["raw_items"], list)
    assert isinstance(out["raw_issues"], list)
    assert isinstance(out["final_items"], list)
    assert isinstance(out["final_issues"], list)
