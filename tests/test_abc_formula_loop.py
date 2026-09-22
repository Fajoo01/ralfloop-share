import json
from pathlib import Path

from openshell_backend.skills import abc_memory
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


def test_every_evidence_rule_has_versioned_weight():
    weights = afl.load_weights()
    missing = {rule.weight_key for rule in afl.EVIDENCE_RULES} - set(weights)
    assert missing == set()


def test_mind_reading_non_pesa_come_fatto(tmp_path: Path):
    result = _score("Arianna sta male per AntonLuca.", tmp_path)
    inference = next(item for item in result["evidence"] if item["kind"] == "inference")

    assert inference["id"] == "mind_reading_non_supportato"
    assert abs(inference["weight"]) <= afl.max_observed_fact_weight() * 0.30
    assert "mind_reading_risk" in result["bias_flags"]


def test_formula_scoring_input_ignores_raw_audit_history(tmp_path: Path):
    report = """
Raw audit storico: AntonLuca e opacita terzo sono citati fuori dallo scoring.

## formula_scoring_input

### current_facts

Arianna ha chiamato Fabio per la bestia.
Fabio non e stato invitato nella scena sociale.
"""
    result = _score(report, tmp_path)

    assert "opacita_terzo" not in {item["id"] for item in result["evidence"]}
    assert "mancato_invito_sociale" in {item["id"] for item in result["evidence"]}



def test_negated_third_mentions_do_not_trigger_opacity(tmp_path: Path):
    result = _score("Arianna non ha parlato di AntonLuca. Nessun terzo nominato.", tmp_path)

    assert "opacita_terzo" not in {item["id"] for item in result["evidence"]}


def test_positive_antonluca_pressure_still_triggers_opacity(tmp_path: Path):
    result = _score("AntonLuca resta pressione attiva e nodo opaco.", tmp_path)

    assert "opacita_terzo" in {item["id"] for item in result["evidence"]}

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



def test_private_repair_delta_scores_as_autonomous_evidence(tmp_path: Path):
    report = """
Fabio non e stato invitato nella scena sociale esterna.
Campo domestico presente ma non normalizzato socialmente.
Arianna ha scritto e ha detto che stava pensando di scrivere lei: ancora viva.
Bestia come gancio logistico.
Micro-riparazione privata post-esclusione sociale.
Dopo il gancio ombrello Arianna riapre il canale privato Fabio.
Cena 1:1 domestica a casa di Fabio per circa un'ora.
Racconta contenuti personali del ritiro.
Contatto sul braccio non respinto.
Nessun terzo nominato.
"""
    result = _score(report, tmp_path)
    ids = {item["id"] for item in result["evidence"]}

    assert "micro_riparazione_privata" in ids
    assert "cena_1_1_domestica" in ids
    assert "contenuti_personali_1_1" in ids
    assert "contatto_tollerato" in ids
    assert "opacita_terzo" not in ids
    assert 52 <= result["prudential_score"] <= 55
    assert result["action"] == "do_nothing_active"


def test_private_repair_does_not_cancel_social_exclusion_cap(tmp_path: Path):
    result = _score(
        "Fabio non e stato invitato nella scena sociale. Micro-riparazione privata con cena 1:1 domestica.",
        tmp_path,
    )

    assert "social_exclusion_cap" in result["bias_flags"]
    assert result["action"] not in {"light_open", "available_for_reconnection"}


def test_raw_food_question_does_not_score_without_structured_interpretation(tmp_path: Path):
    result = _score(
        'Arianna dice: "Tu hai mangiato?". Arianna dice: "Io ho solo frutta".',
        tmp_path,
    )

    assert "auto_invito_implicito_cibo" not in {item["id"] for item in result["evidence"]}


def test_structured_auto_invito_implicito_scores(tmp_path: Path):
    report = """
```json
{
  "quote": "Tu hai mangiato?",
  "speaker": "Arianna",
  "context": "Fabio offered umbrella/profumeria help after rain",
  "interpretation": "Arianna shifts the frame from logistics to food/dinner",
  "evidence_id": "auto_invito_implicito_cibo",
  "confidence": 0.8,
  "counter_evidence": [],
  "notes": "Valid only with food/dinner continuation."
}
```
"""
    result = _score(report, tmp_path)
    item = next(item for item in result["evidence"] if item["id"] == "auto_invito_implicito_cibo")

    assert item["weight"] == 2.0
    assert item["structured_interpretation"]["speaker"] == "Arianna"


def test_structured_auto_invito_requires_arianna_and_context(tmp_path: Path):
    report = """
```json
{
  "quote": "Tu hai mangiato?",
  "speaker": "Fabio",
  "context": "neutral isolated question",
  "interpretation": "Food question",
  "evidence_id": "auto_invito_implicito_cibo",
  "confidence": 0.9,
  "counter_evidence": [],
  "notes": "No Arianna agency and no logistical bridge."
}
```
"""
    result = _score(report, tmp_path)

    assert "auto_invito_implicito_cibo" not in {item["id"] for item in result["evidence"]}


def test_structured_food_opening_removes_logistica_pura(tmp_path: Path):
    report = """
Bestia come gancio logistico.

```json
[
  {
    "quote": "Tu hai mangiato?",
    "speaker": "Arianna",
    "context": "Bestia and umbrella/profumeria logistical bridge after rain",
    "interpretation": "Arianna shifts the frame from logistics to dinner",
    "evidence_id": "auto_invito_implicito_cibo",
    "confidence": 0.8,
    "counter_evidence": [],
    "notes": "Io ho solo frutta reinforces the food opening."
  },
  {
    "quote": "Il passaggio a cena nasce dal gancio ombrello/profumeria.",
    "speaker": "Arianna",
    "context": "Bestia and umbrella/profumeria logistical bridge after rain",
    "interpretation": "The logistical hook becomes dinner and conviviality.",
    "evidence_id": "logistica_convertita_in_convivialita",
    "confidence": 0.75,
    "counter_evidence": [],
    "notes": "Support evidence, not valid without structured food opening."
  }
]
```
"""
    result = _score(report, tmp_path)
    ids = {item["id"] for item in result["evidence"]}
    by_id = {item["id"]: item for item in result["evidence"]}

    assert "auto_invito_implicito_cibo" in ids
    assert "logistica_convertita_in_convivialita" in ids
    assert by_id["logistica_convertita_in_convivialita"]["weight"] == 1.5
    assert "logistica_pura" not in ids


def test_structured_auto_invito_accepts_reported_indirect_quote(tmp_path: Path):
    report = """
```json
{
  "quote": "Arianna chiede se Fabio avesse gia mangiato; Arianna dice di avere solo frutta.",
  "speaker": "Arianna",
  "context": "Fabio offers umbrella/profumeria help after rain",
  "interpretation": "Arianna shifts the frame from logistics to dinner",
  "evidence_id": "auto_invito_implicito_cibo",
  "confidence": 0.8,
  "counter_evidence": [],
  "notes": "Derived from bounded interpretation table."
}
```
"""
    result = _score(report, tmp_path)

    assert "auto_invito_implicito_cibo" in {item["id"] for item in result["evidence"]}


def test_formula_uses_merged_context(tmp_path: Path):
    mem = tmp_path / "memory"
    run = tmp_path / "run"
    full_patch = """Aggiornamento ABC 2026-06-28

## Fatti nuovi

- Bestia come gancio logistico.
- Fabio non e stato invitato a evento sociale.
- Mancata inclusione sociale reale.
- Mattia, amico cinese, tre pizze e Fabio sulla soglia/finestra restano fatti osservati.

## Elementi da interpretare

- Possibile esclusione sociale strutturata, da non assolutizzare.

## Operativo

- do_nothing_active.
"""
    abc_memory.save_telegram_patch(
        mem,
        run,
        raw_text=f"rl:abc:\n{full_patch}",
        payload=full_patch,
        message_id=1,
        sender_id=2,
        created_at="2026-06-28T20:00:00+00:00",
    )
    abc_memory.save_telegram_patch(
        mem,
        run,
        raw_text="rl:abc:\n21:40 circa i 2 amici sono andati ed erano arrivati alle 20:40 circa",
        payload="21:40 circa i 2 amici sono andati ed erano arrivati alle 20:40 circa",
        message_id=2,
        sender_id=2,
        created_at="2026-06-28T21:40:00+00:00",
    )
    context = abc_memory.build_current_context(mem, run)
    result = afl.score_text(
        context["merged_current_context"],
        memory_dir=mem,
        evidence_cache_path=tmp_path / "last_evidence.json",
    )
    evidence_ids = {item["id"] for item in result["evidence"]}

    assert "logistica_pura" in evidence_ids
    assert "mancato_invito_sociale" in evidence_ids
    assert "21:40 circa" in context["merged_current_context"]
    assert "social_exclusion_cap_temporal_attenuation" in context["attenuations"]


def test_third_presence_with_affection_and_overnight_has_stronger_bounded_delta(tmp_path: Path):
    result = _score(
        """
## formula_scoring_input

### factual_micro_deltas
- Alle 23:30 Arianna arriva con Marco. Marco e persona terza presente.
- Effusioni osservate, baci e contatto fisico caldo.
- Marco entra in casa e pernotta.
""",
        tmp_path,
    )
    assessment = result["third_delta_assessment"]

    assert assessment["third_presence_observed"] is True
    assert assessment["affection_or_overnight_positive"] is True
    assert assessment["prudential_delta"] <= -2.5
    assert result["prudential_score"] < result["base_scores_before_bounded_delta"]["prudential_score"]


def test_third_presence_with_no_affection_no_overnight_autonomy_is_small_delta(tmp_path: Path):
    result = _score(
        """
## formula_scoring_input

### current_facts

Micro-riparazione privata. Cena 1:1 domestica. Contenuti personali. Contatto sul braccio non respinto.
Campo domestico e campo quotidiano attivi. Messaggio spontaneo e messaggio leggero: ancora viva.
Due chiacchiere e apertura camper come progetto futuro pratico.
Fabio non e stato invitato nella scena sociale.

### factual_micro_deltas
- Alle 23:30 Arianna arriva in moto con Marco dietro.
- Marco e persona terza presente ma passeggero.
- Arianna risulta alla guida e gestisce lei la moto.
- Marco appare passivo e guarda il telefono.
- Nessuna effusione osservata, nessun bacio, nessun abbraccio.
- Marco non pernotta e non rimane a dormire.
- Nel vocale precedente Arianna non aveva nominato Marco; timeline ambigua perche il vocale era precedente.
""",
        tmp_path,
    )
    assessment = result["third_delta_assessment"]

    assert result["action"] == "do_nothing_active"
    assert assessment["third_presence_observed"] is True
    assert assessment["third_presence_late_evening"] is True
    assert assessment["no_affection_observed"] is True
    assert assessment["no_overnight"] is True
    assert assessment["subject_self_driving_or_autonomous"] is True
    assert assessment["third_passive_or_low_support"] is True
    assert assessment["net_effect"] == "small_prudential_negative"
    assert -1.8 <= assessment["prudential_delta"] <= -0.4
    assert "third_delta_bounded_assessment" in result["bias_flags"]


def test_third_only_named_not_observed_has_no_delta(tmp_path: Path):
    result = _score(
        """
## formula_scoring_input

### factual_micro_deltas
- Arianna nomina una persona esterna in un racconto, senza presenza fisica osservata e senza rientro con lei.
""",
        tmp_path,
    )
    assessment = result["third_delta_assessment"]

    assert assessment["third_presence_observed"] is False
    assert assessment["prudential_delta"] == 0.0
    assert "third_delta_bounded_assessment" not in result["bias_flags"]


def test_ambiguous_prior_omission_is_partial_transparency_not_lie(tmp_path: Path):
    result = _score(
        """
## formula_scoring_input

### factual_micro_deltas
- Alle 23:30 Arianna arriva con Marco dietro: Marco e persona terza presente.
- Nel vocale precedente Arianna non aveva nominato Marco.
- Timeline ambigua: il vocale era precedente alla scena osservata.
- Nessuna effusione osservata e Marco non pernotta.
""",
        tmp_path,
    )
    assessment = result["third_delta_assessment"]

    assert assessment["third_omitted_from_prior_narrative"] is True
    assert assessment["timeline_ambiguous"] is True
    assert assessment["classification"] == "partial_transparency_not_lie"


def test_social_exclusion_cap_remains_with_bounded_third_delta(tmp_path: Path):
    result = _score(
        """
## formula_scoring_input

### current_facts
Micro-riparazione privata. Cena 1:1 domestica. Contenuti personali. Contatto sul braccio non respinto.
Campo domestico e campo quotidiano attivi. Messaggio spontaneo e messaggio leggero: ancora viva.
Fabio non e stato invitato nella scena sociale.

### factual_micro_deltas
- Alle 23:30 Arianna arriva con Marco dietro: persona terza presente.
- Nessuna effusione osservata.
- Marco non pernotta.
""",
        tmp_path,
    )

    assert "social_exclusion_cap" in result["bias_flags"]
    assert result["third_delta_assessment"]["third_presence_observed"] is True
    assert result["action"] == "do_nothing_active"


def test_next_morning_repair_offsets_late_third_presence_without_erasing_it(tmp_path: Path):
    result = _score(
        """
## formula_scoring_input

### current_facts
Micro-riparazione privata. Cena 1:1 domestica. Contenuti personali. Contatto sul braccio non respinto.
Campo domestico e campo quotidiano attivi. Messaggio spontaneo e messaggio leggero: ancora viva.
Fabio non e stato invitato nella scena sociale.

### factual_micro_deltas
- Alle 23:30 Arianna arriva in moto ingessata con Marco dietro: persona terza presente.
- Nel vocale precedente Arianna non aveva nominato Marco; timeline ambigua perche il vocale era precedente.
- Nessuna effusione osservata, nessun bacio, nessun contatto fisico caldo.
- Marco non entra, non pernotta e non rimane a dormire.
- Arianna guidava la moto e Marco era passeggero.
- La mattina successiva alle 7:25 Arianna chiama Fabio fuori.
- Arianna spiega che Marco non sapeva guidare la Vespa; questo spiega perche guidasse lei.
- Fabio chiede se puo abbracciarla; Arianna accetta l'abbraccio.
- L'abbraccio appare naturale, non respinto, non rigido e non escalation romantica piena.
""",
        tmp_path,
    )
    assessment = result["third_delta_assessment"]

    assert assessment["third_presence_observed"] is True
    assert assessment["third_omitted_from_prior_narrative"] is True
    assert assessment["next_morning_repair"] is True
    assert assessment["functional_explanation"] is True
    assert assessment["accepted_hug_repair"] is True
    assert assessment["affection_or_overnight_positive"] is False
    assert assessment["net_effect"] == "small_prudential_negative_with_repair_offset"
    assert assessment["prudential_delta"] == -0.4
    assert result["action"] == "do_nothing_active"
    assert "social_exclusion_cap" in result["bias_flags"]


def test_llm_evidence_extractor_captures_20260708_family_evening_delta(tmp_path: Path):
    baseline = _score(
        """
## formula_scoring_input

### current_facts
Micro-riparazione privata. Cena 1:1 domestica. Contenuti personali. Contatto sul braccio non respinto.
Campo domestico e campo quotidiano attivi. Messaggio spontaneo e messaggio leggero: ancora viva.
Apertura camper come progetto futuro pratico.
Fabio non e stato invitato nella scena sociale esterna. social_exclusion_cap resta attivo.
Investimento familiare e logistica affettiva gia genericamente etichettati.
""",
        tmp_path / "baseline",
    )
    result = _score(
        """
## formula_scoring_input

### current_facts
Micro-riparazione privata. Cena 1:1 domestica. Contenuti personali. Contatto sul braccio non respinto.
Campo domestico e campo quotidiano attivi. Messaggio spontaneo e messaggio leggero: ancora viva.
Apertura camper come progetto futuro pratico.
Fabio non e stato invitato nella scena sociale esterna. social_exclusion_cap resta attivo.
Investimento familiare e logistica affettiva gia genericamente etichettati.

### event_2026_07_08_sera
- Chiamata dalla finestra.
- Richiesta stampa.
- Stampa + accompagnamento spedizione.
- Autoinvito da tua madre.
- Mangiare davvero e si convinto al mangiare.
- Permanenza 20:14-21:16, circa 1h.
- Scioltezza familiare.
- Proposta mele in giardino.
- Limite corporeo solo situazionale: quando arrabbiata/stressata dice non toccare e il contatto fisico peggiora.
- Pressione terzo non aumenta; nessuna persona terza presente.
""",
        tmp_path / "event",
    )
    ids = {item["id"] for item in result["evidence"]}
    atom_ids = {item["id"] for item in result["llm_evidence_extraction"]["evidence_atoms"]}

    assert {
        "autoinvito_familiare",
        "permanenza_familiare_reale",
        "gancio_futuro_domestico",
        "conversione_logistica_in_presenza",
        "boundary_limite_contatto_attivazione",
    } <= ids
    assert atom_ids <= ids
    assert result["prudential_score"] > baseline["prudential_score"]
    assert 60 <= result["prudential_score"] <= 64
    assert result["action"] == "do_nothing_active"
    assert "social_exclusion_cap" in result["bias_flags"]
    assert result["third_delta_assessment"]["third_presence_observed"] is False
    assert "third_delta_bounded_assessment" not in result["bias_flags"]
    assert result["llm_runtime_scoring"] is False
    assert result["llm_extraction_used"] is True
    assert result["llm_evidence_extraction"]["bounded_delta_suggestion"]["prudential_delta"] == 0.0
    assert "event_underweighted_generic_domestic_label" in result["llm_evidence_extraction"]["warnings"]
    assert sum(1 for item in result["evidence"] if item["id"] == "autoinvito_familiare") == 1
    boundary = next(item for item in result["evidence"] if item["id"] == "boundary_limite_contatto_attivazione")
    assert boundary["weight"] < 0


def test_llm_partial_extraction_cannot_remove_deterministic_atoms(tmp_path: Path, monkeypatch):
    def partial_llm(_report_text, _weights):
        return {
            "event_id": "event_partial_llm",
            "facts": ["LLM saw only AntonLuca."],
            "interpretations": [],
            "counter_evidence": [],
            "evidence_atoms": [
                {
                    "id": "relazione_aperta_rifiutata_da_arianna",
                    "kind": "observed_fact",
                    "confidence": 0.9,
                    "supporting_facts": ["Arianna non accetta la relazione aperta."],
                    "cap_interactions": ["third_pressure_down"],
                }
            ],
            "bounded_delta_suggestion": {"prudential_delta": 0.0, "reason": ""},
            "warnings": ["partial_llm_test"],
        }

    monkeypatch.setattr(afl, "_extract_semantic_atoms_via_ollama", partial_llm)
    result = _score(
        """
## formula_scoring_input

### current_facts
Fabio non e stato invitato nella scena sociale esterna. social_exclusion_cap resta attivo.
Investimento familiare e logistica affettiva gia genericamente etichettati.

### event_2026_07_08_sera
- Chiamata dalla finestra.
- Richiesta stampa.
- Stampa + accompagnamento spedizione.
- Autoinvito da tua madre.
- Mangiare davvero e si convinto al mangiare.
- Permanenza 20:14-21:16, circa 1h.
- Scioltezza familiare.
- Proposta mele in giardino.
- Limite corporeo solo situazionale: quando arrabbiata/stressata dice non toccare e il contatto fisico peggiora.

### event_2026_07_09_sera
Arianna dice che AntonLuca vuole una relazione aperta. Arianna non accetta la relazione aperta.
""",
        tmp_path,
    )
    ids = {item["id"] for item in result["evidence"]}

    assert {
        "autoinvito_familiare",
        "permanenza_familiare_reale",
        "gancio_futuro_domestico",
        "conversione_logistica_in_presenza",
        "boundary_limite_contatto_attivazione",
        "relazione_aperta_rifiutata_da_arianna",
    } <= ids
    assert "partial_llm_test" in result["llm_evidence_extraction"]["warnings"]
    assert "llm_semantic_extractor_merged_non_destructive" in result["llm_evidence_extraction"]["warnings"]
