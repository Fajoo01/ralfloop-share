import json
from pathlib import Path

from openshell_backend.skills import abc_formula_loop as afl


CURRENT_CASE = """
Arianna ha dato a Fabio il casco prima di andare a truccare una sposa.
Fabio dice in tono leggero: mi porti fuori stasera?
Arianna e tornata intorno alle 18.
Alle 22:06 Arianna ha chiamato Fabio per la bestia e ha proposto due chiacchiere.
La bestia funziona da gancio logistico ma il contatto ha riaperto il canale.
Arianna e l'amica sono uscite al Nama e Fabio non e stato invitato.
Fabio resta nel campo domestico e nel campo quotidiano dei micro-contatti,
ma non ancora incluso di default nelle uscite sociali esterne.
Fabio percepisce tono non allegro e deve non inseguire.
"""


def _score(text: str, tmp_path: Path):
    return afl.score_text(
        text,
        memory_dir=tmp_path / "abc_memory",
        evidence_cache_path=tmp_path / "last_evidence.json",
        force_extract=False,
    )


def test_determinismo(tmp_path: Path):
    report = "Arianna propone due chiacchiere. Nama senza invitare Fabio."
    first = _score(report, tmp_path)
    second = _score(report, tmp_path)

    assert first == second
    assert (tmp_path / "last_evidence.json").exists()
    json.dumps(second, ensure_ascii=False)


def test_mind_reading_non_pesa_come_fatto(tmp_path: Path):
    result = _score("Arianna sta male per AntonLuca.", tmp_path)
    inference = next(item for item in result["evidence"] if item["kind"] == "inference")

    assert inference["id"] == "mind_reading_non_supportato"
    assert abs(inference["weight"]) <= afl.max_observed_fact_weight() * 0.30
    assert "mind_reading_risk" in result["bias_flags"]


def test_due_chiachiere_non_scatena_azione(tmp_path: Path):
    result = _score("Arianna propone esplicitamente due chiacchiere.", tmp_path)
    item = next(item for item in result["evidence"] if item["id"] == "due_chiacchiere")

    assert item["weight"] < 10
    assert result["prudential_score"] < 70
    assert result["action"] == "do_nothing_active"


def test_mancato_invito_sociale(tmp_path: Path):
    result = _score("Arianna va al Nama senza invitare Fabio.", tmp_path)
    item = next(item for item in result["evidence"] if item["id"] == "mancato_invito_sociale")

    assert item["weight"] == -7
    assert result["raw_score"] >= 43
    assert "social_exclusion_cap" in result["bias_flags"]


def test_contraddizioni_abbassano_confidence(tmp_path: Path):
    without = _score("Arianna propone due chiacchiere.", tmp_path / "a")
    with_contradiction = _score(
        "Arianna propone due chiacchiere ma non chiarisce e resta silenzio diretto sul nodo.",
        tmp_path / "b",
    )

    assert with_contradiction["confidence"] < without["confidence"]
    assert "contradiction_present" in with_contradiction["bias_flags"]


def test_azione_prudente_sotto_soglia(tmp_path: Path):
    result = _score("Arianna propone due chiacchiere. Messaggio leggero: ancora viva.", tmp_path)

    assert result["prudential_score"] < 70
    assert result["action"] not in {"ask_for_clarification", "light_open"}
    assert result["action"] in {"monitor_only", "do_nothing_active"}


def test_nessuna_inferenza_come_prova(tmp_path: Path):
    result = _score("Lei pensa che Fabio voglia farmi ingelosire. Tono non allegro.", tmp_path)
    inference_items = [item for item in result["trace"] if item["kind"] == "inference"]

    assert inference_items
    assert all(item["kind"] == "inference" for item in inference_items)
    assert result["action"] not in {"light_open", "available_for_reconnection"}


def test_soglia_chiusura_romantica(tmp_path: Path):
    result = _score("Arianna propone due chiacchiere. Ha scritto ancora viva.", tmp_path)

    assert result["prudential_score"] < 85
    assert result["action"] != "ask_for_clarification_romantic"


def test_caso_corrente_range_58_65(tmp_path: Path):
    """Con micro-aperture e limiti sociali, RLFULL deve essere tra 58 e 65."""
    result = _score(CURRENT_CASE, tmp_path)

    assert 58 <= result["rlfull_current"] <= 65


def test_prudential_non_supera_65_con_limiti(tmp_path: Path):
    """Con mancato invito sociale e mind_reading_risk, prudential <= 65."""
    result = _score(CURRENT_CASE, tmp_path)

    assert result["prudential_score"] <= 65
    assert "mind_reading_risk" in result["bias_flags"]
    assert "social_exclusion_cap" in result["bias_flags"]


def test_confidence_scende_con_inferenze(tmp_path: Path):
    """Se ci sono inferenze non osservabili, confidence <= 0.85."""
    result = _score(CURRENT_CASE, tmp_path)

    assert result["confidence"] <= 0.85


def test_action_resta_do_nothing_active(tmp_path: Path):
    """Nel range 56-65, l'azione deve restare do_nothing_active."""
    result = _score(CURRENT_CASE, tmp_path)

    assert 56 <= result["rlfull_current"] <= 65
    assert result["action"] == "do_nothing_active"
