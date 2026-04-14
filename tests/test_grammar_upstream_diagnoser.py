from openshell_backend.skills.responses import build_skill_insufficient_response


def test_grammar_upstream_diagnostic_changes_diagnosis_kind() -> None:
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

    diagnosis = payload["autofix_candidate"]["diagnosis"]
    assert diagnosis["kind"] == "grammar_upstream_issue"
    assert diagnosis["target_file"] == "openshell_backend/skill_grammar_rag.py"
    assert diagnosis["target_symbol"] == "_simple_local_grammar_fallback"
