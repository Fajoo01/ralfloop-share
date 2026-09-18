import json

import pytest
from pydantic import ValidationError

from ralfloop_agent.teacher.knowledge import (
    CurriculumGraph,
    HintStep,
    KnowledgeUnit,
    ReviewStatus,
    SourceRef,
    knowledge_unit_from_curriculum,
)
from ralfloop_agent.teacher.pedagogy import (
    AccessibilitySupport,
    EducationLevel,
    EvidenceType,
    KnowledgeState,
    LanguageLevel,
    LanguageProfile,
    LearnerProfile,
    LiteracyProfile,
    SessionMode,
    select_pedagogy,
    update_knowledge_state,
)
from ralfloop_agent.teacher.service import TeacherService
from ralfloop_agent.teacher.store import TeacherStore
from ralfloop_agent.teacher.web.learning import Curriculum


def low_literacy_profile():
    return LearnerProfile(
        age_band="adult",
        education_level=EducationLevel.EMERGENT_LITERACY,
        language_profile=LanguageProfile(
            l1=["ar"], l2="it", framework="LASLLIAM",
            oral_comprehension=LanguageLevel.A2,
            oral_production=LanguageLevel.A1,
            reading=LanguageLevel.PRE_A1,
            writing=LanguageLevel.PRE_A1,
        ),
        literacy_profile=LiteracyProfile(decoding=0.1, functional_literacy=0.15),
        preferred_interaction_modes=["voice", "choice"],
    )


def test_l2_low_literacy_is_audio_first_without_lowering_subject_level():
    decision = select_pedagogy(low_literacy_profile(), action="explain", subject="storia", topic="Costituzione")
    assert decision.mode is SessionMode.LITERACY_L2
    assert decision.access.audio_first
    assert decision.access.speech_to_text
    assert decision.target_sentences == 2
    assert "adatta l'accesso" in decision.prompt_contract()


def test_dsa_style_support_is_accessibility_not_diagnosis():
    profile = LearnerProfile(
        education_level=EducationLevel.UPPER,
        accessibility_support=AccessibilitySupport(
            text_to_speech=True, short_lines=True, line_focus=True,
            synchronized_highlight=True, alternative_response_modes=True,
        ),
    )
    decision = select_pedagogy(profile, action="explain")
    assert decision.access.line_focus and decision.access.short_lines
    assert decision.max_response_chars == 1200
    assert "Non diagnosticare" in decision.prompt_contract()


def test_scholar_mode_routes_to_deep_grounded_path():
    profile = LearnerProfile(education_level=EducationLevel.MASTER)
    decision = select_pedagogy(profile, action="summarize_material", material_supplied=True)
    assert decision.mode is SessionMode.SCHOLAR
    assert decision.model_path.value == "deep"
    assert decision.require_grounding
    assert decision.max_response_chars >= 6000


def test_claim_check_preserves_l2_access_strategy():
    decision = select_pedagogy(
        low_literacy_profile(),
        action="explain",
        subject="italiano L2",
        topic="Azioni quotidiane",
        student_move="claim_check",
    )
    assert decision.mode is SessionMode.LITERACY_L2
    assert decision.strategy.value == "oral_rehearsal"
    assert decision.access.audio_first is True


def test_claim_check_preserves_scholar_argument_critique():
    decision = select_pedagogy(
        LearnerProfile(education_level=EducationLevel.MASTER),
        action="explain",
        subject="analisi matematica",
        topic="Derivata e monotonia",
        student_move="claim_check",
    )
    assert decision.mode is SessionMode.SCHOLAR
    assert decision.strategy.value == "argument_critique"
    assert decision.model_path.value == "deep"


def test_bounded_mastery_weights_transfer_more_than_recognition():
    start = KnowledgeState(component="fractions.equivalence", probability=0.2)
    recognition = update_knowledge_state(start, correct=True, evidence_type=EvidenceType.RECOGNITION, now=100)
    transfer = update_knowledge_state(start, correct=True, evidence_type=EvidenceType.TRANSFER, now=100)
    assert 0.2 < recognition.probability < transfer.probability < 1
    assert transfer.due_at > 100
    wrong = update_knowledge_state(transfer, correct=False, evidence_type=EvidenceType.TRANSFER, now=200)
    assert wrong.probability < transfer.probability


def test_knowledge_unit_quality_gate_and_curriculum_graph():
    source = SourceRef(source_id="mim", title="Fonte", source_class="authoritative")
    base = KnowledgeUnit(
        concept_id="a", title="A", learning_objectives=["Capire A"],
        source_refs=[source], license="CC-BY", review_status=ReviewStatus.HUMAN_REVIEWED,
        hint_ladder=[HintStep(level=0, kind="orientation", text="Da dove inizi?")],
    )
    child = KnowledgeUnit(
        concept_id="b", title="B", prerequisites=["a"], learning_objectives=["Usare B"],
        source_refs=[source], license="CC-BY", review_status=ReviewStatus.HUMAN_REVIEWED,
    )
    graph = CurriculumGraph([base, child])
    assert graph.prerequisites_for("b") == ["a"]
    assert graph.next_concepts("a") == ["b"]
    with pytest.raises(ValidationError):
        KnowledgeUnit(concept_id="bad", title="Bad", review_status=ReviewStatus.HUMAN_REVIEWED)


def test_existing_curriculum_converts_to_reviewed_knowledge_units():
    curriculum = Curriculum()
    unit = knowledge_unit_from_curriculum(curriculum.topics["fractions"])
    assert unit.concept_id == "fractions"
    assert unit.learning_objectives
    assert unit.review_status is ReviewStatus.HUMAN_REVIEWED


def test_service_persists_profile_and_injects_deterministic_policy(tmp_path):
    captured = {}

    def model(system_prompt, user_prompt):
        captured["system"] = system_prompt
        captured["payload"] = json.loads(user_prompt)
        return {"response": "Ascolta: scegli una delle due opzioni."}

    store = TeacherStore(tmp_path / "teacher.sqlite3")
    service = TeacherService(store, model_call=model)
    profile = low_literacy_profile().model_dump(mode="json")
    student = service.login("CARD-L2", "adulto", "", learner_profile=profile)["student"]
    session = service.start_session(student["student_id"], "italiano L2", "saluti quotidiani")["session"]
    result = service.explain(session["session_id"], "Come saluto il vicino al mattino?")

    assert result["pedagogy"]["mode"] == "literacy_l2"
    assert captured["payload"]["student"]["learner_profile"]["language_profile"]["reading"] == "pre-A1"
    assert captured["payload"]["pedagogy"]["access"]["audio_first"] is True
    assert "DECISIONE PEDAGOGICA DETERMINISTICA" in captured["system"]

    persisted = service.login("CARD-L2")["student"]
    assert persisted["preferences"]["learner_profile"]["education_level"] == "emergent_literacy"


def test_l2_vague_reference_clarifies_without_math_example(tmp_path):
    def model(*_):
        raise AssertionError("LLM should not guess a vague reference")

    store = TeacherStore(tmp_path / "teacher.sqlite3")
    service = TeacherService(store, model_call=model)
    profile = low_literacy_profile().model_dump(mode="json")
    student = service.login("CARD-L2-VAGUE", "adulto", "", learner_profile=profile)["student"]
    session = service.start_session(student["student_id"], "italiano L2", "azioni quotidiane")["session"]

    result = service.explain(session["session_id"], "Come si dice questa azione?")

    assert result["deterministic"] is True
    assert result["pedagogy"]["mode"] == "literacy_l2"
    assert "quale" in result["response"].casefold()
    assert "-3 - 5" not in result["response"]




def test_counterexample_uses_core_evidence_without_llm(tmp_path):
    class Core:
        def classify_turn(self, text):
            assert "fazzoletto" in text
            return {"ok": True, "move": "counterexample", "signal": "counterexample_cue",
                    "confidence": 0.93, "writes": 0, "external_side_effects": 0}

        def concept_evidence(self, topic):
            assert topic == "Gli stati dell'acqua"
            return {"ok": True, "found": True, "topic": topic,
                    "evidence": "Un solido può essere flessibile; un liquido fluisce spontaneamente e deformarsi non basta.",
                    "misconceptions": "Il ghiaccio è solo l'esempio dello stato solido dell'acqua.",
                    "writes": 0, "external_side_effects": 0}

    def model(*_):
        raise AssertionError("LLM should not be called for curated counterexample")

    service = TeacherService(
        TeacherStore(tmp_path / "teacher.sqlite3"),
        model_call=model,
        deterministic_core=Core(),
    )
    student = service.login("COUNTEREXAMPLE", "primary", "4")["student"]
    session = service.start_session(
        student["student_id"], "scienze", "Gli stati dell'acqua"
    )["session"]
    result = service.explain(
        session["session_id"],
        "ma un fazzoletto prende la forma del contenitore ma non è liquido cosa c'entra il ghiaccio",
        context="Consegna: associa ghiaccio, acqua nel bicchiere e vapore a solido, liquido e gas.",
    )

    assert result["deterministic"] is True
    assert result["pedagogy"]["strategy"] == "error_analysis"
    assert result["core_evidence"]["turn_classification"]["signal"] == "counterexample_cue"
    assert "flessibile" in result["response"]
    assert "fluisce spontaneamente" in result["response"]
    assert "regola corretta" in result["response"]


def test_claim_shaped_turn_uses_curated_evidence_without_llm(tmp_path):
    class Core:
        def classify_turn(self, text):
            return {"ok": True, "move": "neutral", "signal": "none", "confidence": 0.55}

        def concept_evidence(self, topic):
            assert topic == "Le stagioni"
            return {
                "ok": True,
                "found": True,
                "topic": topic,
                "evidence": "Le stagioni dipendono soprattutto dall'inclinazione dell'asse terrestre.",
                "misconceptions": "La distanza Terra-Sole non è la causa principale delle stagioni.",
            }

    def model(*_):
        raise AssertionError("claim-shaped turn with curated evidence must not call the LLM")

    service = TeacherService(
        TeacherStore(tmp_path / "claim.sqlite3"),
        model_call=model,
        deterministic_core=Core(),
    )
    student = service.login("CLAIM", "middle", "2")["student"]
    session = service.start_session(student["student_id"], "scienze", "Le stagioni")["session"]
    result = service.explain(
        session["session_id"],
        "In estate fa caldo perché la Terra è più vicina al Sole.",
    )
    assert result["deterministic"] is True
    assert result["pedagogy"]["strategy"] == "error_analysis"
    assert "inclinazione" in result["response"]
    assert "distanza Terra-Sole" in result["response"]


def test_counterexample_targeting_prefers_specific_rare_terms(tmp_path):
    class Core:
        def classify_turn(self, text):
            return {"ok": True, "move": "counterexample", "signal": "counterexample_cue", "confidence": 0.93}

        def concept_evidence(self, topic):
            return {
                "ok": True,
                "found": True,
                "topic": topic,
                "evidence": (
                    "Un fazzoletto è un solido flessibile che può adattarsi a un contenitore. "
                    "Un liquido fluisce e assume la forma del recipiente. "
                    "La sabbia è formata da granuli solidi che possono scorrere collettivamente."
                ),
                "misconceptions": "Scorrere o prendere la forma del contenitore non basta per essere un liquido.",
            }

    def model(*_):
        raise AssertionError("specific counterexample should use curated evidence")

    service = TeacherService(
        TeacherStore(tmp_path / "specific-target.sqlite3"),
        model_call=model,
        deterministic_core=Core(),
    )
    student = service.login("SPECIFIC", "middle", "2")["student"]
    session = service.start_session(student["student_id"], "scienze", "Gli stati dell'acqua")["session"]
    result = service.explain(
        session["session_id"],
        "E la sabbia allora scorre e prende la forma del barattolo: è un liquido?",
    )
    assert result["deterministic"] is True
    assert "sabbia" in result["response"].casefold()
    assert "granuli" in result["response"].casefold()


def test_open_question_uses_targeted_curated_sentence_when_supported(tmp_path):
    class Core:
        def classify_turn(self, text):
            return {"ok": True, "move": "question", "signal": "question_form", "confidence": 0.82}

        def concept_evidence(self, topic):
            return {
                "ok": True,
                "found": True,
                "topic": topic,
                "evidence": (
                    "La Luna riflette la luce del Sole. "
                    "Le fasi lunari dipendono dalla geometria tra Sole, Terra e Luna: "
                    "vediamo una falce quando è visibile solo una parte della metà illuminata."
                ),
                "misconceptions": "Le fasi non sono causate dall'ombra della Terra.",
            }

    def model(*_):
        raise AssertionError("supported open question should use curated evidence")

    service = TeacherService(
        TeacherStore(tmp_path / "open-question.sqlite3"),
        model_call=model,
        deterministic_core=Core(),
    )
    student = service.login("OPEN", "primary", "5")["student"]
    session = service.start_session(student["student_id"], "scienze", "Luna e luce")["session"]
    result = service.explain(
        session["session_id"],
        "Allora perché a volte vedo solo una falce?",
    )
    assert result["deterministic"] is True
    assert "falce" in result["response"]
    assert "geometria" in result["response"]


def test_unsupported_open_question_still_uses_model(tmp_path):
    calls = []

    class Core:
        def classify_turn(self, text):
            return {"ok": True, "move": "question", "signal": "question_form", "confidence": 0.82}

        def concept_evidence(self, topic):
            return {
                "ok": True,
                "found": True,
                "topic": topic,
                "evidence": "La Luna riflette la luce del Sole.",
                "misconceptions": "La Luna non emette luce visibile propria.",
            }

    def model(system_prompt, user_prompt):
        calls.append(json.loads(user_prompt))
        return {"response": "La distanza media è circa 384 mila chilometri."}

    service = TeacherService(
        TeacherStore(tmp_path / "unsupported-open.sqlite3"),
        model_call=model,
        deterministic_core=Core(),
    )
    student = service.login("UNSUPPORTED", "primary", "5")["student"]
    session = service.start_session(student["student_id"], "scienze", "Luna e luce")["session"]
    result = service.explain(
        session["session_id"],
        "Quanto dista in media la Luna dalla Terra?",
    )
    assert result.get("deterministic") is not True
    assert len(calls) == 1


def test_plain_question_uses_curated_evidence_when_directly_supported(tmp_path):
    class Core:
        def classify_turn(self, text):
            return {"ok": True, "move": "question", "signal": "question_form",
                    "confidence": 0.82, "writes": 0, "external_side_effects": 0}

        def concept_evidence(self, topic):
            return {"ok": True, "found": True, "topic": topic,
                    "evidence": "Un liquido fluisce spontaneamente; deformarsi non basta.",
                    "misconceptions": "Non usare la forma del contenitore come unico criterio.",
                    "writes": 0, "external_side_effects": 0}

    def model(*_):
        raise AssertionError("directly supported concept question should not call the LLM")

    service = TeacherService(
        TeacherStore(tmp_path / "binding.sqlite3"),
        model_call=model,
        deterministic_core=Core(),
    )
    student = service.login("BINDING", "primary", "4")["student"]
    session = service.start_session(
        student["student_id"], "scienze", "Gli stati dell'acqua"
    )["session"]
    result = service.explain(session["session_id"], "Come riconosco un liquido?")

    assert result["deterministic"] is True
    assert "fluisce spontaneamente" in result["response"]
    assert result["core_evidence"]["concept_evidence"]["found"] is True

def test_confusion_uses_curated_evidence_without_llm(tmp_path):
    class Core:
        def classify_turn(self, text):
            return {"ok": True, "move": "confusion", "signal": "confusion_cue", "confidence": 0.94}

        def concept_evidence(self, topic):
            return {
                "ok": True,
                "found": True,
                "topic": topic,
                "evidence": (
                    "Dividere per una frazione non nulla equivale a moltiplicare per il suo reciproco. "
                    "Il reciproco è il numero che, moltiplicato per il divisore, dà 1."
                ),
                "misconceptions": "Non capovolgere una frazione ogni volta che la incontri.",
            }

    def model(*_):
        raise AssertionError("confusion on curated topic should not call LLM")

    service = TeacherService(
        TeacherStore(tmp_path / "confusion.sqlite3"),
        model_call=model,
        deterministic_core=Core(),
    )
    student = service.login("CONFUSION", "primary", "5")["student"]
    session = service.start_session(
        student["student_id"], "matematica", "Divisione fra frazioni"
    )["session"]
    result = service.explain(session["session_id"], "non ho capito")

    assert result["deterministic"] is True
    assert "Ripartiamo da un solo punto sicuro" in result["response"]
    assert "reciproco" in result["response"]
    assert "Quale parola o passaggio" in result["response"]


def test_history_is_selective_but_preserves_grounded_source(tmp_path):
    calls = []

    def model(system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        calls.append(payload)
        return {"response": f"Risposta {len(calls)}"}

    service = TeacherService(
        TeacherStore(tmp_path / "compact-history.sqlite3"),
        model_call=model,
    )
    student = service.login("COMPACT", "university", "2")["student"]
    session = service.start_session(student["student_id"], "filosofia", "testo")['session']
    material = "Fonte vincolante: la regola richiede eccezioni dichiarate."
    service.summarize_material(session['session_id'], material, "Riassumi.")
    for index in range(1, 6):
        service.explain(session['session_id'], f"Domanda libera numero {index}?")

    history = calls[-1]["history"]
    assert len(history) <= 4
    assert any(
        isinstance(item.get("student_request"), dict)
        and material in item["student_request"].get("material", "")
        for item in history
    )
    questions = [
        item.get("student_request", {}).get("question", "")
        for item in history
        if isinstance(item.get("student_request"), dict)
    ]
    assert "Domanda libera numero 1?" not in questions
    assert "Domanda libera numero 4?" in questions


def test_request_example_uses_curated_example_without_llm(tmp_path):
    class Core:
        def classify_turn(self, text):
            return {"ok": True, "move": "request_example", "signal": "example_request", "confidence": 0.96}

        def concept_evidence(self, topic):
            return {
                "ok": True,
                "found": True,
                "topic": topic,
                "evidence": (
                    "Dividere per una frazione non nulla equivale a moltiplicare per il suo reciproco. "
                    "Esempio concreto: quante porzioni da 1/4 stanno in 1/2? Ce ne stanno 2."
                ),
                "misconceptions": "Non capovolgere una frazione ogni volta che la incontri.",
            }

    def model(*_):
        raise AssertionError("curated example should not call the LLM")

    service = TeacherService(
        TeacherStore(tmp_path / "example.sqlite3"),
        model_call=model,
        deterministic_core=Core(),
    )
    student = service.login("EXAMPLE", "primary", "5")["student"]
    session = service.start_session(
        student["student_id"], "matematica", "Divisione fra frazioni"
    )["session"]
    result = service.explain_differently(
        session["session_id"],
        "ancora non capisco. niente regole, fammi un esempio concreto",
    )

    assert result["deterministic"] is True
    assert "porzioni da 1/4" in result["response"]
    assert "esempio concreto" in result["response"].casefold()


def test_repetition_guard_retries_with_latest_turn_context(tmp_path):
    calls = []
    repeated = "Usa sempre la stessa regola e ripeti lo stesso procedimento passo per passo."

    def model(system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        calls.append(payload)
        if len(calls) <= 2:
            return {"response": repeated}
        assert payload["quality_retry"]["reason"] == "repetitive_response"
        assert "secondo esempio" in payload["quality_retry"]["latest_student_turn"]
        return {"response": "Cambio strategia: nel secondo esempio guardiamo prima il caso concreto richiesto."}

    service = TeacherService(
        TeacherStore(tmp_path / "retry.sqlite3"),
        model_call=model,
    )
    student = service.login("RETRY", "middle", "2")["student"]
    session = service.start_session(student["student_id"], "scienze", "tema libero")["session"]
    service.explain(session["session_id"], "Spiegami la regola.")
    result = service.explain(session["session_id"], "Fammi un secondo esempio diverso.")

    assert len(calls) == 3
    assert result["quality_retry"]["reason"] == "repetitive_response"
    assert result["quality_retry"]["fallback"] is False
    assert result["response"].startswith("Cambio strategia")


def test_repetition_guard_falls_back_instead_of_repeating_again(tmp_path):
    repeated = "Ripeto esattamente la stessa spiegazione perché non cambio strategia."

    def model(*_):
        return {"response": repeated}

    service = TeacherService(
        TeacherStore(tmp_path / "retry-fallback.sqlite3"),
        model_call=model,
    )
    student = service.login("RETRY-FALLBACK", "middle", "2")["student"]
    session = service.start_session(student["student_id"], "scienze", "tema libero")["session"]
    service.explain(session["session_id"], "Spiegami il concetto.")
    result = service.explain(session["session_id"], "Non ho capito, spiegalo di nuovo.")

    assert result["quality_retry"]["fallback"] is True
    assert result["response"] != repeated
    assert "non voglio ripeterla" in result["response"].casefold()


def test_service_injects_bounded_multi_turn_history_and_source_material(tmp_path):
    calls = []

    def model(system_prompt, user_prompt):
        payload = json.loads(user_prompt)
        calls.append(payload)
        if payload["action"] == "summarize_material":
            return {"response": "Il testo dice che la regola richiede eccezioni dichiarate."}
        return {"response": "Non posso attribuire al testo un autore che non è indicato."}

    service = TeacherService(
        TeacherStore(tmp_path / "history.sqlite3"),
        model_call=model,
    )
    student = service.login("HISTORY", "university", "2")["student"]
    session = service.start_session(
        student["student_id"], "filosofia", "Argomentazione da testo fornito"
    )["session"]
    material = (
        "Nel testo l'autore sostiene che una regola è giustificata soltanto "
        "quando le sue eccezioni sono dichiarate."
    )
    service.summarize_material(
        session["session_id"], material, "Riassumi senza aggiunte."
    )
    service.explain(
        session["session_id"], "Quale criterio usa il testo per giustificare una regola?"
    )

    assert len(calls) == 2
    assert "history" not in calls[0]
    history = calls[1]["history"]
    assert len(history) == 1
    assert history[0]["action"] == "summarize_material"
    assert history[0]["student_request"]["material"] == material
    assert "eccezioni dichiarate" in history[0]["tutor_response"]
    assert calls[1]["pedagogy"]["require_grounding"] is True


def test_grounded_material_abstains_from_unsupported_attribution(tmp_path):
    calls = []

    def model(system_prompt, user_prompt):
        calls.append(json.loads(user_prompt))
        return {"response": "Il testo dice che la regola richiede eccezioni dichiarate."}

    service = TeacherService(
        TeacherStore(tmp_path / "grounded-abstention.sqlite3"),
        model_call=model,
    )
    student = service.login("GROUND-ABSTAIN", "university", "2")["student"]
    session = service.start_session(
        student["student_id"], "filosofia", "Argomentazione da testo fornito"
    )["session"]
    material = (
        "Nel testo l'autore sostiene che una regola è giustificata soltanto "
        "quando le sue eccezioni sono dichiarate."
    )
    service.summarize_material(
        session["session_id"], material, "Riassumi senza aggiunte."
    )
    result = service.explain(
        session["session_id"], "Quale filosofo intende allora?"
    )

    assert len(calls) == 1
    assert result["deterministic"] is True
    assert result["source_mode"] == "provided_material"
    assert "non c'è abbastanza informazione" in result["response"].casefold()


def test_deterministic_followup_prefers_new_relevant_evidence(tmp_path):
    class Core:
        def classify_turn(self, text):
            move = "counterexample" if "macchina" in text else "question"
            return {"ok": True, "move": move, "signal": "test", "confidence": 0.9}

        def concept_evidence(self, topic):
            return {
                "ok": True, "found": True, "topic": topic,
                "evidence": (
                    "Se la velocità è costante, l'accelerazione è zero e la forza risultante è zero. "
                    "In macchina la trazione e le resistenze si bilanciano: la forza risultante resta zero."
                ),
                "misconceptions": "Non confondere una singola forza con la forza risultante.",
            }

    def model(*_):
        raise AssertionError("curated follow-up must not call the LLM")

    service = TeacherService(TeacherStore(tmp_path / "novel-evidence.sqlite3"), model_call=model, deterministic_core=Core())
    student = service.login("NOVEL", "middle", "2")["student"]
    session = service.start_session(student["student_id"], "fisica", "Forza, accelerazione e velocità")["session"]
    first = service.explain(session["session_id"], "Se la velocità è costante, che cosa dice F=ma?")
    second = service.explain(session["session_id"], "Ma in macchina la trazione non sparisce: come funziona?")

    assert first["deterministic"] is True and second["deterministic"] is True
    assert second["response"] != first["response"]
    assert "resistenze" in second["response"]


def test_repeated_grounded_abstention_changes_wording_without_ungrounding(tmp_path):
    calls = []

    def model(system_prompt, user_prompt):
        calls.append(json.loads(user_prompt))
        return {"response": "Il testo parla soltanto di una regola e delle sue eccezioni."}

    service = TeacherService(TeacherStore(tmp_path / "ground-repeat.sqlite3"), model_call=model)
    student = service.login("GROUND-REPEAT", "university", "2")["student"]
    session = service.start_session(student["student_id"], "filosofia", "Argomentazione da testo fornito")["session"]
    material = "Nel testo l'autore sostiene che una regola è giustificata soltanto quando le sue eccezioni sono dichiarate."
    service.summarize_material(session["session_id"], material, "Riassumi senza aggiunte.")
    first = service.explain(session["session_id"], "Quindi sta parlando sicuramente di Popper?")
    second = service.explain(session["session_id"], "Quale filosofo intende allora?")

    assert len(calls) == 1
    assert first["source_mode"] == second["source_mode"] == "provided_material"
    assert first["response"] != second["response"]
    assert "informazioni esterne" in second["response"].casefold()


def test_repeated_percent_hint_changes_strategy_without_solution_leak(tmp_path):
    class Core:
        def math_hint(self, text, attempt):
            return {
                "ok": True, "recognized": True, "kind": "percent_discount",
                "hint": "Fai un solo passo: usa 1 - 25/100 e moltiplica per 80. Fermati prima del prezzo finale.",
                "allow_final_solution": False,
            }

    def model(*_):
        raise AssertionError("recognized percent hint must not call the LLM")

    service = TeacherService(TeacherStore(tmp_path / "percent-repeat.sqlite3"), model_call=model, deterministic_core=Core())
    student = service.login("PERCENT-REPEAT", "middle", "2")["student"]
    session = service.start_session(student["student_id"], "matematica", "Percentuali")["session"]
    exercise = "Una maglietta costa 80 euro e ha il 25% di sconto. Qual è il prezzo finale?"
    first = service.hint(session["session_id"], exercise, "Dimmi il prezzo")
    second = service.hint(session["session_id"], exercise, "Dammi solo il risultato")

    assert first["response"] != second["response"]
    assert "importo dello sconto" in second["response"]
    assert "60" not in first["response"] and "60" not in second["response"]


def test_l2_counterexample_keeps_access_mode_but_uses_error_analysis():
    decision = select_pedagogy(
        low_literacy_profile(), action="explain", subject="scienze",
        topic="materia", student_move="counterexample",
    )
    assert decision.mode is SessionMode.LITERACY_L2
    assert decision.strategy.value == "error_analysis"
    assert decision.access.audio_first is True


def test_web_state_persists_profile_and_knowledge_component(tmp_path):
    from ralfloop_agent.teacher.web.state import State

    state = State(tmp_path / "student.sqlite3", clock=lambda: 1000.0)
    student = state.register("UNIVERSAL", "Credential!123", "Adult Learner", "adult", 1)
    profile = low_literacy_profile().model_dump(mode="json")
    saved = state.set_learner_profile(student, profile)
    assert state.learner_profile(student) == saved

    first = state.update_kc_mastery(student, "italiano.saluti", True, EvidenceType.RECALL)
    second = state.update_kc_mastery(student, "italiano.saluti", False, EvidenceType.TRANSFER)
    assert first["probability"] > 0.15
    assert second["probability"] < first["probability"]


def test_web_profile_api_is_authenticated_and_strict(tmp_path):
    from fastapi.testclient import TestClient
    from ralfloop_agent.teacher.web.api import create_app
    from ralfloop_agent.teacher.web.client import DemoTeacher
    from ralfloop_agent.teacher.web.state import State

    state = State(tmp_path / "student.sqlite3")
    state.register("PROFILE", "Credential!123", "Profile Demo", "middle", 2, demo=True)
    client = TestClient(create_app(state, DemoTeacher(), origin="http://testserver"), raise_server_exceptions=False)
    client.headers.update({"origin": "http://testserver", "x-teacher-request": "1"})
    assert client.get("/api/learner-profile").status_code == 401
    assert client.post("/api/login", json={"membership_card_id": "PROFILE", "credential": "Credential!123"}).status_code == 200
    profile = client.get("/api/learner-profile").json()
    profile["accessibility_support"]["short_lines"] = True
    profile["accessibility_support"]["line_focus"] = True
    assert client.post("/api/learner-profile", json=profile).status_code == 200
    assert client.get("/api/learner-profile").json()["accessibility_support"]["line_focus"] is True
    bad = {**profile, "diagnosis": "dyslexia"}
    assert client.post("/api/learner-profile", json=bad).status_code == 422


def test_local_qwen_uses_server_side_schema_and_fast_deep_routing(monkeypatch):
    from ralfloop_agent.teacher.service import ScheduledQwenModel

    monkeypatch.setenv("RALF_LLAMA_CPP_BASE_URL", "http://127.0.0.1:19091")
    monkeypatch.setenv("RALF_TEACHER_FAST_MODEL", "teacher-fast")
    monkeypatch.setenv("RALF_TEACHER_DEEP_MODEL", "teacher-deep")
    model = ScheduledQwenModel(scheduler=object())
    try:
        fmt = model.fast_provider.request_options["response_format"]
        assert fmt["type"] == "json_schema"
        assert fmt["json_schema"]["strict"] is True
        assert fmt["json_schema"]["schema"]["required"] == ["response"]
        assert model._provider_for(json.dumps({"pedagogy": {"model_path": "fast"}})).default_model == "teacher-fast"
        assert model._provider_for(json.dumps({"pedagogy": {"model_path": "deep"}})).default_model == "teacher-deep"
        assert model.deep_provider.request_options["max_tokens"] > model.fast_provider.request_options["max_tokens"]
    finally:
        model.close()


def test_scholar_material_without_school_keywords_maps_to_academic_study(tmp_path):
    from ralfloop_agent.teacher.web.application import LearningApplication
    from ralfloop_agent.teacher.web.client import DemoTeacher
    from ralfloop_agent.teacher.web.state import State

    state = State(tmp_path / "scholar.sqlite3")
    student_id = state.register("SCHOLAR", "Credential!123", "Scholar Demo", "master", 1)
    student = state.authenticate(state.login("SCHOLAR", "Credential!123"))
    student["learner_profile"] = state.learner_profile(student)
    app = LearningApplication(state, DemoTeacher())
    material = app.add_material(student, "Paper", "A methodological comparison with no school-topic keywords.", "own", "document")
    assert material["topics"] == ["academic_study"]
    assert app.curriculum.require(student, "academic_study")["subject"] == "studio accademico"


def test_web_help_stream_and_reader_contract(tmp_path):
    from fastapi.testclient import TestClient
    from ralfloop_agent.teacher.web.api import create_app
    from ralfloop_agent.teacher.web.client import DemoTeacher
    from ralfloop_agent.teacher.web.state import State

    state = State(tmp_path / "stream.sqlite3")
    state.register("STREAM", "Credential!123", "Stream Demo", "middle", 2, demo=True)
    client = TestClient(create_app(state, DemoTeacher(), origin="http://testserver"), raise_server_exceptions=False)
    client.headers.update({"origin": "http://testserver", "x-teacher-request": "1"})
    assert client.post("/api/login", json={"membership_card_id": "STREAM", "credential": "Credential!123"}).status_code == 200
    activity = client.post("/api/activities", json={"topic": "fractions", "activity_type": "matching"}).json()
    response = client.post(f"/api/activities/{activity['activity_id']}/help/stream", json={"mode": "hint", "question": ""})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert events[0]["type"] == "delta"
    assert events[-1]["type"] == "done"
    source = client.get("/assets/app.js").text
    assert "/help/stream" in source
    assert "Reader e accessibilità" in source


def test_content_pipeline_enforces_provenance_and_bounded_evidence():
    from ralfloop_agent.teacher.content_pipeline import ContentPipeline, ContentSource
    from ralfloop_agent.teacher.web.learning import Curriculum

    curriculum = Curriculum()
    student = {"school_level": "middle", "grade": 2, "school_track": ""}
    source = ContentSource(
        source_id="oer:demo",
        title="Frazioni OER",
        text=("Le frazioni hanno numeratore e denominatore. " * 80),
        source_class="oer",
        license="CC-BY-4.0",
        human_reviewed=True,
    )
    record = ContentPipeline(curriculum).ingest(student, source)
    assert record.trusted is True
    assert "fractions" in record.topic_ids
    assert max(len(chunk.text) for chunk in record.chunks) <= 1200
    pack = ContentPipeline.evidence_pack(record, "numeratore denominatore", max_chunks=2)
    assert len(pack["chunks"]) <= 2
    assert pack["source"]["source_id"] == "oer:demo"
    with pytest.raises(ValidationError):
        ContentSource(source_id="bad", title="No license", text="x", source_class="oer")
