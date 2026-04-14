from openshell_backend.skills.responses import build_skill_insufficient_response


def test_grammar_skill_response_includes_upstream_diagnostic_when_raw_issues_exist() -> None:
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
    assert "upstream_diagnostic" in af
    assert af["upstream_diagnostic"]["kind"] == "grammar_analysis_suspicions"
    assert af["upstream_diagnostic"]["raw_issues"][0]["token"] == "dall'Ikea"


def test_non_grammar_skill_response_does_not_include_upstream_diagnostic() -> None:
    payload = build_skill_insufficient_response(
        skill_name="other",
        user_goal="ciao",
        final_answer="nope",
        validation={"raw_issues": [{"kind": "x"}]},
    )

    af = payload["autofix_candidate"]
    assert "upstream_diagnostic" not in af
