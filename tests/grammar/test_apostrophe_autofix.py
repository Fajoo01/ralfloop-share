from openshell_backend.skill_grammar_rag import maybe_answer_grammar_request
import json


def test_apostrophe_token_autofixes_to_split_tokens() -> None:
    out = maybe_answer_grammar_request("Analisi grammaticale: Devi ordinare lo scolapiatti dall'Ikea")
    assert out is not None

    data = json.loads(out)
    tokens = [x["token"] for x in data]
    cats = {x["token"]: x["categoria"] for x in data}

    assert "dall'Ikea" not in tokens
    assert "dall'" in tokens
    assert "Ikea" in tokens
    assert cats["dall'"] == "preposizione_articolata"
    assert cats["Ikea"] == "nome_proprio"
