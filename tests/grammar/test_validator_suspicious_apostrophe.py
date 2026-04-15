from openshell_backend.skills.validators import validate_grammar_output, is_grammar_output_sufficient


def test_validate_grammar_output_rejects_suspicious_apostrophe_token() -> None:
    payload = """[
      {"token":"Devi","categoria":"verbo"},
      {"token":"ordinare","categoria":"verbo"},
      {"token":"lo","categoria":"articolo_determinativo"},
      {"token":"scolapiatti","categoria":"nome_comune"},
      {"token":"dall'Ikea","categoria":"nome_comune"}
    ]"""

    result = validate_grammar_output(payload)

    assert result["ok"] is False
    assert result["reason"] == "grammar_suspicions_found"
    assert isinstance(result.get("raw_issues"), list)
    assert result["raw_issues"][0]["kind"] == "suspicious_apostrophe_token"


def test_is_grammar_output_sufficient_rejects_suspicious_apostrophe_token() -> None:
    payload = """[
      {"token":"Devi","categoria":"verbo"},
      {"token":"ordinare","categoria":"verbo"},
      {"token":"lo","categoria":"articolo_determinativo"},
      {"token":"scolapiatti","categoria":"nome_comune"},
      {"token":"dall'Ikea","categoria":"nome_comune"}
    ]"""
    assert is_grammar_output_sufficient(payload) is False
