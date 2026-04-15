from openshell_backend.skills.responses import build_skill_insufficient_response


def test_grammar_upstream_issue_includes_coder_handoff_prompt() -> None:
    payload = build_skill_insufficient_response(
        skill_name="grammar",
        user_goal="Analisi grammaticale: Devi ordinare lo scolapiatti dall'Ikea",
        final_answer="""[{"token":"dall'Ikea","categoria":"nome_comune"}]""",
        validation={
            "raw_items": [{"token": "dall'Ikea", "categoria": "nome_comune"}],
            "raw_issues": [
                {
                    "kind": "suspicious_apostrophe_token",
                    "token": "dall'Ikea",
                    "categoria": "nome_comune",
                    "reason": "token con apostrofo classificato come nome_comune",
                    "suggested_fix": "split_apostrophe_token_before_classification",
                }
            ],
            "final_items": [
                {"token": "dall'", "categoria": "preposizione_articolata"},
                {"token": "Ikea", "categoria": "nome_proprio"},
            ],
            "final_issues": [],
        },
    )

    af = payload["autofix_candidate"]
    assert "coder_patch_candidate" in af
    assert "coder_handoff_prompt" in af
    prompt = af["coder_handoff_prompt"]
    assert "Target file: openshell_backend/skill_grammar_rag.py" in prompt
    assert "Target symbol: _simple_local_grammar_fallback" in prompt
    assert "suspicious_apostrophe_token" in prompt
