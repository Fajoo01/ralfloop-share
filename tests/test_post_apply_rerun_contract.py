from pathlib import Path

from openshell_backend.skill_grammar_rag import maybe_answer_grammar_request
from openshell_backend.skills.validators import validate_skill_output, is_skill_output_sufficient


def test_post_apply_rerun_contract_on_current_grammar_skill() -> None:
    answer = maybe_answer_grammar_request(
        "Analisi grammaticale: Devi ordinare lo scolapiatti dall'Ikea"
    )
    assert answer is not None

    validation = validate_skill_output("grammar", answer)
    ok = is_skill_output_sufficient("grammar", answer)

    assert isinstance(validation, dict)
    assert isinstance(ok, bool)
