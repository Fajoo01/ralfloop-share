from __future__ import annotations

import json

from openshell_backend.skills.validators import validate_grammar_output


def test_validate_grammar_output_accepts_generic_entries() -> None:
    payload = json.dumps([
        {"token": "Il", "categoria": "articolo_determinativo", "genere": "maschile", "numero": "singolare"},
        {"token": "gatto", "categoria": "nome_comune"},
        {"token": "nero", "categoria": "aggettivo_qualificativo"},
        {"token": "corre", "categoria": "verbo"},
        {"token": ".", "categoria": "segno_punteggiatura"},
    ], ensure_ascii=False)

    result = validate_grammar_output(payload)

    assert result["ok"] is True
    assert result["reason"] == "sufficient"


def test_validate_grammar_output_rejects_missing_categoria() -> None:
    payload = json.dumps([
        {"token": "Il"},
        {"token": "gatto"},
    ], ensure_ascii=False)

    result = validate_grammar_output(payload)

    assert result["ok"] is False
    assert result["reason"] in {"no_valid_grammar_entries", "missing_required_fields"}


def test_validate_grammar_output_rejects_invalid_json() -> None:
    result = validate_grammar_output("not-json")
    assert result["ok"] is False
    assert result["reason"] == "invalid_json"
