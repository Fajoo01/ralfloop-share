from __future__ import annotations

import difflib
import json
import os
import re
from collections import Counter
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

from ralfloop_agent.providers.chat import (
    ChatProviderSettings,
    OpenAICompatibleChatProvider,
)
from ralfloop_agent.providers.gpu_engine_scheduler import (
    TransactionalGpuScheduler,
)

from .store import TeacherStore
from .pedagogy import (
    SessionMode, default_learner_profile, profile_from_student, select_pedagogy,
)


PEDAGOGY_PROMPT = """
Sei l'insegnante digitale del doposcuola Tiremm Innanz.

Obiettivo: aiutare lo studente a capire e diventare autonomo.

Regole:
- adatta lessico, esempi e difficoltà all'età e alla classe;
- non infantilizzare;
- distingui errori concettuali da errori di calcolo o scrittura;
- se lo studente porta un controesempio, un'obiezione o segnala che una spiegazione non torna, rispondi PRIMA a quel punto preciso;
- in quel caso dichiara se l'osservazione è corretta, parzialmente corretta o errata e spiega il perché senza eluderla;
- se il controesempio mostra che una regola precedente era troppo semplificata, correggi esplicitamente la regola invece di difenderla;
- distingui una proprietà che può verificarsi da una proprietà definitoria: non usare un singolo indizio superficiale come criterio sufficiente, soprattutto in matematica e scienze;
- non ripartire dalla lezione generale finché non hai risolto l'obiezione specifica dello studente;
- preferisci spiegazione, verifica della comprensione, suggerimento,
  procedimento e infine soluzione;
- non dare automaticamente la risposta finale a un esercizio;
- se viene richiesto un hint, dai il minimo aiuto utile;
- se show_solution è false, non rivelare il risultato finale
  quando lo studente può ancora arrivarci;
- per matematica e scienze controlla i passaggi;
- non affermare di aver letto materiali non forniti;
- quando lavori su materiale fornito, non aggiungere come fatti
  informazioni assenti dal materiale;
- non inventare fonti;
- se il contesto contiene grammar_evidence, trattalo come evidenza lessicale/sintattica read-only: più analisi dello stesso token sono alternative contestuali, non scegliere arbitrariamente;
- se deterministic_evidence contiene concept_evidence, trattala come evidenza concettuale curata e read-only: non contraddirla e usala per correggere semplificazioni o misconcezioni;
- nei dati grammaticali, i frame valenziali approvati sono evidenza scolastica più forte; i pattern T-PAS sono candidati semantici utili ma non una decisione automatica sul contesto;
- rispondi nella lingua usata dallo studente salvo richiesta diversa.
- prima di rispondere, rileggi e correggi ortografia, grammatica, concordanze, forme verbali, accenti e punteggiatura;
- non inserire accidentalmente parole di altre lingue salvo che siano richieste o necessarie;
- nel campo "response" usa testo semplice ben impaginato: paragrafi brevi e liste numerate quando utili, senza Markdown, HTML o prefissi come response=;

Restituisci esclusivamente un oggetto JSON.
Il campo "response" contiene il testo da mostrare allo studente.
"""


def _student_turn(payload: dict[str, Any]) -> str:
    for key in ("question", "concept", "student_attempt", "student_answer"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _claim_like_for_evidence(text: str, student_move: str) -> bool:
    """Return True for claim-shaped turns that should be checked against curated evidence."""
    if student_move in {"counterexample", "correction"}:
        return True
    if student_move in {"confusion", "request_example"}:
        return False
    normalized = " ".join(text.casefold().split())
    if not normalized:
        return False
    if not normalized.endswith("?"):
        return True
    if normalized.endswith(("giusto?", "vero?", "no?")):
        return True
    open_question_prefixes = (
        "come ", "perché ", "perche ", "cosa ", "quale ", "quali ",
        "chi ", "dove ", "quando ", "quanto ", "quanta ", "quanti ",
        "quante ", "allora perché ", "allora perche ",
    )
    if normalized.startswith(open_question_prefixes):
        return False
    # Yes/no and conditional questions usually contain a proposition to validate.
    return True


_CONCEPT_STOPWORDS = {
    "allora", "anche", "ancora", "avere", "come", "cosa", "della", "delle",
    "dello", "degli", "dalla", "dalle", "dallo", "dopo", "essere", "fatto",
    "nella", "nelle", "nello", "negli", "perche", "perché", "posso", "puo",
    "può", "quale", "quindi", "questo", "questa", "quello", "quella", "solo",
    "sono", "stesso", "stessa", "tutto", "tutta", "vero", "giusto",
}


def _concept_term(word: str) -> str:
    value = word.casefold().strip("'’")
    groups = (
        (("division", "dividere", "dividi", "diviso", "divisore"), "divid"),
        (("capovol",), "capovol"),
        (("derivat",), "derivat"),
        (("intervall",), "intervall"),
        (("emisfer",), "emisfer"),
        (("stagion",), "stagion"),
        (("falc",), "falc"),
        (("lumin",), "lumin"),
        (("distan", "vicin"), "distan"),
        (("disordin",), "disordin"),
        (("metafor",), "metafor"),
        (("indic",), "indic"),
        (("reciproc",), "reciproc"),
        (("frazion",), "frazion"),
        (("probabil",), "probabil"),
        (("indipenden",), "indipenden"),
        (("incompatibil",), "incompatibil"),
        (("costant",), "costant"),
        (("cambi",), "cambi"),
        (("andar", "andat"), "and"),
        (("somm",), "somm"),
    )
    for prefixes, stem in groups:
        if any(value.startswith(prefix) for prefix in prefixes):
            return stem
    if len(value) >= 7:
        return value[:6]
    return value


def _concept_terms(text: str) -> set[str]:
    words = re.findall(r"[A-Za-zÀ-ÿ0-9']+", text.casefold())
    return {
        _concept_term(word)
        for word in words
        if len(word) >= 4 and word not in _CONCEPT_STOPWORDS
    }


def _targeted_concept_text(
    text: str,
    query: str,
    *,
    topic: str = "",
    max_sentences: int = 2,
    previous_response: str = "",
) -> tuple[str, int]:
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text.strip()) if part.strip()]
    if not sentences:
        return text.strip(), 0
    query_terms = _concept_terms(query) - _concept_terms(topic)
    sentence_terms = [_concept_terms(sentence) for sentence in sentences]
    query_words = re.findall(r"[A-Za-zÀ-ÿ0-9']+", query.casefold())
    query_bigrams = set(zip(query_words, query_words[1:]))
    document_frequency = Counter(
        term for terms in sentence_terms for term in (query_terms & terms)
    )
    ranked: list[tuple[int, float, int, str]] = []
    wants_reason = query.casefold().lstrip().startswith(("perché", "perche"))
    for index, sentence in enumerate(sentences):
        overlap = query_terms & sentence_terms[index]
        # Prefer terms that identify this specific follow-up rather than generic
        # topic vocabulary repeated in several evidence sentences.
        score = sum(3 if document_frequency[term] == 1 else 1 for term in overlap)
        sentence_words = re.findall(r"[A-Za-zÀ-ÿ0-9']+", sentence.casefold())
        sentence_bigrams = set(zip(sentence_words, sentence_words[1:]))
        matching_bigrams = query_bigrams & sentence_bigrams
        relevant_bigrams = {
            bigram for bigram in matching_bigrams
            if any(_concept_term(word) in query_terms for word in bigram if len(word) >= 4)
        }
        score += 2 * len(relevant_bigrams)
        if wants_reason and any(cue in sentence.casefold() for cue in ("perché", "perche", "per questo", "perciò", "quindi")):
            score += 2
        novelty = 1.0 - _response_similarity(previous_response, sentence)
        ranked.append((score, novelty, index, sentence))
    best_score = max(score for score, _, _, _ in ranked)
    if best_score <= 0:
        if previous_response and len(sentences) > 1:
            return max(sentences, key=lambda sentence: 1.0 - _response_similarity(previous_response, sentence)), 0
        return sentences[0], 0
    selected = sorted(
        (item for item in ranked if item[0] > 0),
        key=lambda item: (-item[0], -item[1], item[2]),
    )[:max_sentences]
    selected.sort(key=lambda item: item[2])
    return " ".join(sentence for _, _, _, sentence in selected), best_score


def _concept_example(text: str) -> str:
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        candidate = sentence.strip()
        if candidate.casefold().startswith(("esempio:", "esempio concreto:")):
            return candidate
    return ""


def _last_tutor_response(history: list[dict[str, Any]]) -> str:
    for item in reversed(history):
        value = item.get("tutor_response")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _grounded_abstention_response(history: list[dict[str, Any]]) -> str:
    primary = (
        "Nel materiale fornito non c'è abbastanza informazione per "
        "attribuire questa idea a un autore, filosofo o teoria specifica. "
        "Posso restare su ciò che l'estratto sostiene esplicitamente."
    )
    previous = _last_tutor_response(history)
    if previous and _response_similarity(previous, primary) >= 0.78:
        return (
            "L'estratto non indica quale filosofo, autore o teoria sia coinvolto: "
            "identificarlo richiederebbe aggiungere informazioni esterne al testo."
        )
    return primary


def _response_similarity(previous: str, current: str) -> float:
    if not previous or not current:
        return 0.0
    def norm(value: str) -> str:
        return re.sub(r"\s+", " ", value.casefold()).strip()
    return difflib.SequenceMatcher(None, norm(previous), norm(current)).ratio()


def _repetition_fallback(student_move: str) -> str:
    if student_move == "confusion":
        return (
            "La spiegazione precedente si stava ripetendo e non ti stava aiutando. "
            "Dimmi quale parola o passaggio preciso non è chiaro: ripartiamo solo da quello."
        )
    if student_move == "request_example":
        return (
            "La spiegazione precedente si stava ripetendo. Cambio davvero strada: "
            "preferisci un esempio con oggetti quotidiani oppure con numeri piccoli?"
        )
    return (
        "La risposta precedente non affrontava abbastanza il nuovo punto. "
        "Non voglio ripeterla: indicami il passaggio preciso a cui ti riferisci e rispondo solo a quello."
    )


def _needs_reference_clarification(text: str) -> bool:
    low = " ".join(text.casefold().split())
    if not low or len(low.split()) > 12:
        return False
    if not re.search(r"\b(?:quest[oaie]|quell[oaie])\b", low):
        return False
    if re.search(r"\d|[=+*/÷()]", low):
        return False
    # A leading minus sign with no numeral still does not identify the expression.
    return True


def _brevity_request(text: str) -> str:
    low = " ".join(text.casefold().split())
    if "in una frase" in low or "una sola frase" in low:
        return "one_sentence"
    if any(cue in low for cue in ("poche parole", "molto breve", "brevissimo", "brevemente")):
        return "short"
    return ""


def _trim_response(text: str, limit: int) -> str:
    text = " ".join(text.split()).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit].rstrip()
    boundary = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
    if boundary >= max(80, limit // 2):
        return cut[: boundary + 1].strip()
    return cut.rsplit(" ", 1)[0].rstrip(" ,;:") + "."


def _micro_check_suffix(mode: SessionMode, student_move: str) -> str:
    if mode is SessionMode.LITERACY_L2:
        return "Ripeti con parole tue?"
    if student_move in {"counterexample", "correction", "claim_check"}:
        return "Qual è la regola corretta in una frase?"
    if student_move == "confusion":
        return "Quale passaggio resta poco chiaro?"
    return "Ti torna questo passaggio?"


def _last_question(text: str) -> str:
    matches = re.findall(r"(?:^|(?<=[.!]))\s*([^?]{1,220}\?)", text)
    return matches[-1].strip() if matches else ""


def _fit_response_contract(text: str, limit: int, *, suffix: str = "") -> str:
    value = " ".join(text.split()).strip()
    suffix = " ".join(suffix.split()).strip()
    if not suffix:
        return _trim_response(value, limit)
    if len(suffix) >= limit:
        return _trim_response(suffix, limit)
    if value.endswith(suffix) and len(value) <= limit:
        return value
    if value.endswith(suffix):
        value = value[:-len(suffix)].rstrip()
    budget = max(1, limit - len(suffix) - 1)
    body = _trim_response(value, budget) if value else ""
    return (body + " " + suffix).strip() if body else suffix


def _strip_forbidden_final_answer(text: str, payload: dict[str, Any]) -> tuple[str, bool]:
    value = " ".join(text.split()).strip()
    request = " ".join(
        str(payload.get(key) or "")
        for key in ("question", "concept", "exercise", "student_attempt", "student_answer")
    ).strip()
    expressions = re.findall(r"\b\d+(?:[.,]\d+)?\s*(?:[x×*+\-/÷])\s*\d+(?:[.,]\d+)?\b", request)
    forbidden_cues = (
        "la risposta è", "la risposta e", "il risultato è", "il risultato e",
        "la soluzione è", "la soluzione e", "risultato finale", "soluzione finale",
    )
    pieces = re.split(r"(?<=[.!?])\s+", value)
    kept: list[str] = []
    changed = False
    for piece in pieces:
        low = piece.casefold()
        direct = any(cue in low for cue in forbidden_cues)
        if not direct:
            for expr in expressions:
                pattern = re.escape(expr).replace(r"\ ", r"\s*") + r"\s*=\s*[-+]?\d+(?:[.,]\d+)?"
                if re.search(pattern, piece, flags=re.IGNORECASE):
                    direct = True
                    break
        if direct:
            changed = True
            continue
        kept.append(piece)
    cleaned = " ".join(kept).strip()
    if changed and not cleaned:
        cleaned = "Fermiamoci un passo prima del risultato: prova tu il calcolo finale."
    return cleaned or value, changed


def _evidence_safe_response(
    response: str,
    concept_evidence: dict[str, Any] | None,
    *,
    student_text: str,
) -> tuple[str, bool]:
    if not isinstance(concept_evidence, dict):
        return response, False
    facts = str(concept_evidence.get("evidence") or "").strip()
    if not facts:
        return response, False
    # Quando esiste evidence concettuale curata, il modello non è fonte di
    # verità: può aiutare altrove con formulazione e dialogo, ma il corpo
    # fattuale mostrato allo studente viene ricostruito dall'evidence stessa.
    # Questo evita risposte che citano due parole corrette e aggiungono poi
    # una falsa informazione non presente nella fonte deterministica.
    targeted, _ = _targeted_concept_text(
        facts, student_text, topic="", max_sentences=2, previous_response=""
    )
    safe = " ".join((targeted or facts).split()).strip()
    original = " ".join(response.split()).strip()
    return safe, safe != original


def _enforce_model_output_contract(
    response: str,
    *,
    decision: Any,
    action: str,
    payload: dict[str, Any],
    student_move: str,
    concept_evidence: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    value = " ".join(response.replace("**", "").replace("__", "").split()).strip()
    if not value:
        raise RuntimeError("teacher_model_empty_response")
    reasons: list[str] = []

    guarded, evidence_replaced = _evidence_safe_response(
        value, concept_evidence, student_text=_student_turn(payload)
    )
    if evidence_replaced:
        value = guarded
        reasons.append("concept_evidence_pinned")

    if action == "hint" and not bool(decision.allow_final_solution):
        value, stripped = _strip_forbidden_final_answer(value, payload)
        if stripped:
            reasons.append("final_answer_removed")

    suffix = ""
    if bool(decision.micro_check):
        suffix = _last_question(value)
        if not suffix:
            suffix = _micro_check_suffix(decision.mode, student_move)
            reasons.append("micro_check_added")

    before = value
    value = _fit_response_contract(value, int(decision.max_response_chars), suffix=suffix)
    if value != before:
        reasons.append("length_enforced")

    return value, {
        "applied": bool(reasons),
        "reasons": reasons,
        "max_response_chars": int(decision.max_response_chars),
    }


def _student_facing_guard(text: str) -> str:
    value = " ".join(text.split()).strip()
    low = value.casefold()
    prefix = "non dire che "
    if low.startswith(prefix):
        return "Non è corretto dire che " + value[len(prefix):]
    prefix = "non presentare "
    if low.startswith(prefix):
        return "Non è corretto presentare " + value[len(prefix):]
    prefix = "non descrivere "
    if low.startswith(prefix):
        return "Non è corretto descrivere " + value[len(prefix):]
    return value


def _grounded_source_material(history: list[dict[str, Any]]) -> str:
    for item in history:
        request = item.get("student_request")
        if not isinstance(request, dict):
            continue
        material = request.get("material")
        if isinstance(material, str) and material.strip():
            return material.strip()
    return ""


def _grounded_attribution_unsupported(question: str, material: str) -> bool:
    if not question.strip() or not material.strip():
        return False
    low = question.casefold()
    cues = (
        "sicuramente", "sta parlando di", "parla di", "si riferisce a",
        "quale filosofo", "quale autore", "chi intende", "chi sarebbe",
    )
    if not any(cue in low for cue in cues):
        return False
    generic = {
        "autore", "parla", "parlando", "riferisce", "sicuramente", "intende",
        "quale", "quali", "allora", "sarebbe", "testo", "estratto",
    }
    query_terms = _concept_terms(question) - generic
    material_terms = _concept_terms(material)
    unsupported = {
        term for term in query_terms
        if len(term) >= 5 and term not in material_terms
    }
    return bool(unsupported)


def _student_move(core: Any | None, payload: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    text = _student_turn(payload)
    if not text or core is None:
        return "neutral", None
    classify = getattr(core, "classify_turn", None)
    if not callable(classify):
        return "neutral", None
    try:
        evidence = classify(text)
    except Exception:
        return "neutral", None
    if not isinstance(evidence, dict):
        return "neutral", None
    move = str(evidence.get("move") or "neutral")
    allowed = {"neutral", "question", "confusion", "request_example", "counterexample", "correction"}
    if move not in allowed:
        return "neutral", None
    return move, evidence


def _concept_evidence(core: Any | None, topic: str) -> dict[str, Any] | None:
    if core is None or not topic.strip():
        return None
    lookup = getattr(core, "concept_evidence", None)
    if not callable(lookup):
        return None
    try:
        evidence = lookup(topic.strip())
    except Exception:
        return None
    if not isinstance(evidence, dict) or evidence.get("found") is not True:
        return None
    return evidence


_HISTORY_TEXT_LIMITS = {
    "question": 3000,
    "concept": 3000,
    "exercise": 4000,
    "student_attempt": 3000,
    "student_answer": 1200,
    "context": 5000,
    "material": 8000,
    "objective": 2000,
    "topic": 1000,
    "difficulty": 200,
}


def _bounded_request(payload: dict[str, Any]) -> dict[str, Any]:
    bounded: dict[str, Any] = {}
    for key, limit in _HISTORY_TEXT_LIMITS.items():
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            bounded[key] = value.strip()[:limit]
    if isinstance(payload.get("show_solution"), bool):
        bounded["show_solution"] = payload["show_solution"]
    return bounded


def _history_has_grounded_source(history: list[dict[str, Any]]) -> bool:
    for item in history:
        if item.get("source_mode") == "provided_material":
            return True
        request = item.get("student_request")
        if isinstance(request, dict) and isinstance(request.get("material"), str):
            if request["material"].strip():
                return True
    return False


def _history_context(store: TeacherStore, session_id: str) -> list[dict[str, Any]]:
    """Compact conversational memory: latest turns plus the active grounded source."""
    budget = 5200
    try:
        recent = store.recent_events(session_id, limit=12)
    except Exception:
        return []

    def is_grounded(event: dict[str, Any]) -> bool:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return False
        request = payload.get("request")
        return (
            isinstance(request, dict)
            and isinstance(request.get("material"), str)
            and bool(request["material"].strip())
        )

    keep_ids = {
        event.get("event_id")
        for event in recent[-3:]
    }
    grounded = [event for event in recent if is_grounded(event)]
    if grounded:
        keep_ids.add(grounded[-1].get("event_id"))
    events = [event for event in recent if event.get("event_id") in keep_ids]

    selected: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        request = payload.get("request")
        item: dict[str, Any] = {"action": str(event.get("kind") or "")}
        if isinstance(request, dict) and request:
            compact_request = dict(request)
            for key, limit in {
                "question": 1200,
                "concept": 1200,
                "exercise": 1600,
                "student_attempt": 1200,
                "student_answer": 500,
                "context": 1800,
                "material": 3500,
                "objective": 800,
            }.items():
                value = compact_request.get(key)
                if isinstance(value, str):
                    compact_request[key] = value[:limit]
            item["student_request"] = compact_request
        response = payload.get("response")
        if isinstance(response, str) and response.strip():
            item["tutor_response"] = response.strip()[:1200]
        source_mode = payload.get("source_mode")
        if isinstance(source_mode, str) and source_mode:
            item["source_mode"] = source_mode
        if len(item) == 1:
            continue
        encoded = json.dumps(item, ensure_ascii=False)
        if len(encoded) > budget:
            continue
        selected.append(item)
        budget -= len(encoded)
    return selected


def _teacher_system_prompt(
    decision: Any,
    *,
    concept_evidence: dict[str, Any] | None,
    grounding_active: bool,
) -> str:
    prompt = (
        PEDAGOGY_PROMPT
        + "\n\nDECISIONE PEDAGOGICA DETERMINISTICA:\n"
        + decision.prompt_contract()
    )
    if concept_evidence is not None:
        evidence = str(concept_evidence.get("evidence") or "").strip()
        misconceptions = str(concept_evidence.get("misconceptions") or "").strip()
        prompt += (
            "\n\nEVIDENZA CONCETTUALE VINCOLANTE:\n"
            "- Non contraddire i fatti curati seguenti.\n"
            f"- Fatti: {evidence}\n"
            f"- Misconception da non rafforzare: {misconceptions}\n"
            "- Se una tua spiegazione precedente li contraddice, correggila esplicitamente."
        )
    if grounding_active:
        prompt += (
            "\n\nGROUNDING PERSISTENTE VINCOLANTE:\n"
            "- Se la history contiene materiale fornito dallo studente, quel materiale resta la fonte del follow-up.\n"
            "- Non attribuire autore, teoria, posizione o fatti che non compaiono nel materiale.\n"
            "- Se il materiale non basta, dillo esplicitamente invece di completare dalla memoria del modello."
        )
    return prompt


TEACHER_RESPONSE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["response"],
    "properties": {
        "response": {"type": "string", "minLength": 1},
        "correct": {"type": "boolean"},
    },
}


ModelCall = Callable[[str, str], dict[str, Any]]


class TeacherService:
    def __init__(
        self,
        store: TeacherStore,
        *,
        model_call: ModelCall | None = None,
        grammar_evidence: Callable[[str], dict[str, Any] | None] | None = None,
        deterministic_core: Any | None = None,
    ) -> None:
        self.store = store
        self.model_call = model_call or ScheduledQwenModel()
        self.grammar_evidence = grammar_evidence
        self.deterministic_core = deterministic_core

    def login(
        self,
        card_id: str,
        school_level: str | None = None,
        class_year: str | None = None,
        learner_profile: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        validated_profile = None
        if learner_profile is not None:
            validated_profile = default_learner_profile(
                school_level, class_year, override=learner_profile
            ).model_dump(mode="json")
        student = self.store.login(
            card_id,
            school_level=school_level,
            class_year=class_year,
            learner_profile=validated_profile,
        )
        return {
            "ok": True,
            "student": student,
            "card_stored": False,
        }

    def start_session(
        self,
        student_id: str,
        subject: str,
        topic: str = "",
    ) -> dict[str, Any]:
        session = self.store.start_session(
            student_id,
            subject,
            topic,
        )
        student = self.store.student(student_id)
        return {
            "ok": True,
            "session": session,
            "student": student,
        }

    def _grammar_context(
        self,
        session: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if self.grammar_evidence is None:
            return None
        scope = f"{session.get('subject', '')} {session.get('topic', '')}".casefold()
        if not any(word in scope for word in ("ital", "grammat", "lingu", "morfolog", "sintass", "ortograf")):
            return None
        fields = ("question", "concept", "exercise", "student_attempt", "material", "objective", "topic")
        text = " ".join(str(payload.get(key) or "") for key in fields).strip()[:1200]
        if not text:
            return None
        try:
            return self.grammar_evidence(text)
        except Exception:
            return None

    def _decision_for(
        self,
        session: dict[str, Any],
        action: str,
        *,
        material_supplied: bool = False,
        show_solution: bool = False,
    ):
        student = self.store.student(session["student_id"])
        learner = profile_from_student(student)
        decision = select_pedagogy(
            learner,
            action=action,
            subject=session["subject"],
            topic=session["topic"],
            material_supplied=material_supplied,
            show_solution=show_solution,
        )
        return student, learner, decision

    def _core_math_check(
        self,
        session_id: str,
        exercise: str,
        student_answer: str,
        show_solution: bool,
    ) -> dict[str, Any] | None:
        if self.deterministic_core is None:
            return None
        session = self.store.session(session_id)
        scope = f"{session.get('subject', '')} {session.get('topic', '')}".casefold()
        if not any(word in scope for word in ("mat", "aritmet", "algebr", "fraz", "fisic", "chimic")):
            return None
        try:
            evidence = self.deterministic_core.math_check(exercise, student_answer)
        except Exception:
            return None
        if not evidence.get("recognized") or not evidence.get("answer_recognized"):
            return None
        _student, _learner, decision = self._decision_for(
            session, "check_answer", show_solution=show_solution
        )
        correct = bool(evidence.get("equivalent"))
        kind = str(evidence.get("kind") or "")
        if correct:
            response = (
                "Corretto: il valore proposto soddisfa l'equazione."
                if kind == "linear_equation"
                else "Corretto: la risposta è numericamente equivalente al risultato dell'espressione."
            )
        elif show_solution:
            response = (
                f"La risposta non è corretta. Il valore corretto è {evidence.get('expected')}."
                if kind == "linear_equation"
                else f"La risposta non è corretta. Il risultato dell'espressione è {evidence.get('expected')}."
            )
        else:
            response = "La risposta non è corretta."
            if kind == "linear_equation":
                response += (
                    " Controlla il valore proposto sostituendolo nell'equazione originale: "
                    "i due membri non coincidono. Poi riprendi dall'ultimo passaggio corretto "
                    "applicando la stessa operazione a entrambi i membri."
                )
            elif kind == "percent_discount":
                response += (
                    " Controlla separatamente l'importo dello sconto usando la percentuale "
                    "e il prezzo iniziale; poi sottrai quello sconto dal prezzo di partenza. "
                    "Fermati prima del prezzo finale."
                )
            else:
                guidance = ""
                hint_call = getattr(self.deterministic_core, "math_hint", None)
                if callable(hint_call):
                    try:
                        hint_evidence = hint_call(exercise, student_answer)
                    except Exception:
                        hint_evidence = None
                    if isinstance(hint_evidence, dict) and hint_evidence.get("recognized") is True:
                        guidance = str(hint_evidence.get("hint") or "").strip()
                if guidance:
                    response += " " + guidance
                else:
                    response += (
                        " Ricontrolla il prossimo passaggio senza cambiare il risultato a caso."
                    )
        output = {
            "ok": True,
            "action": "check_answer",
            "response": response,
            "correct": correct,
            "source_mode": "deterministic_core",
            "deterministic": True,
            "core_evidence": evidence,
            "pedagogy": decision.model_dump(mode="json"),
        }
        self.store.event(session_id, "check_answer", {
            "response": response,
            "request": _bounded_request({
                "exercise": exercise,
                "student_answer": student_answer,
                "show_solution": show_solution,
            }),
            "source_mode": "deterministic_core",
            "strategy": decision.strategy.value, "mode": decision.mode.value,
            "model_path": "none", "core_tool": "core.math_check",
        })
        return output

    def _core_concept_reply(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if action not in {"explain", "explain_differently"}:
            return None
        session = self.store.session(session_id)
        student_move, turn_evidence = _student_move(
            self.deterministic_core, payload
        )
        student_text = _student_turn(payload)
        if session.get("topic") == "Frazioni equivalenti":
            relation_call = getattr(self.deterministic_core, "fraction_relation", None)
            if callable(relation_call):
                try:
                    relation = relation_call(student_text)
                except Exception:
                    relation = None
                if isinstance(relation, dict) and relation.get("recognized") is True:
                    left = relation.get("left") or {}
                    right = relation.get("right") or {}
                    left_text = f"{left.get('numerator')}/{left.get('denominator')}"
                    right_text = f"{right.get('numerator')}/{right.get('denominator')}"
                    equivalent = bool(relation.get("equivalent"))
                    try:
                        cross_left = int(left.get("numerator")) * int(right.get("denominator"))
                        cross_right = int(right.get("numerator")) * int(left.get("denominator"))
                    except (TypeError, ValueError):
                        cross_left = cross_right = None
                    if cross_left is not None and cross_right is not None:
                        comparison = (
                            f"{left.get('numerator')}×{right.get('denominator')}={cross_left} e "
                            f"{right.get('numerator')}×{left.get('denominator')}={cross_right}"
                        )
                        response = (
                            f"Sì: {left_text} e {right_text} sono equivalenti; {comparison}."
                            if equivalent
                            else f"No: {left_text} e {right_text} non sono equivalenti; {comparison}, quindi i prodotti incrociati sono diversi."
                        )
                    else:
                        response = (
                            f"Sì: {left_text} e {right_text} sono equivalenti perché i prodotti incrociati coincidono."
                            if equivalent
                            else f"No: {left_text} e {right_text} non sono equivalenti perché i prodotti incrociati non coincidono."
                        )
                    student = self.store.student(session["student_id"])
                    learner = profile_from_student(student)
                    decision = select_pedagogy(
                        learner,
                        action=action,
                        subject=session["subject"],
                        topic=session["topic"],
                        student_move=student_move,
                    )
                    if decision.micro_check:
                        history = _history_context(self.store, session_id)
                        if _last_tutor_response(history):
                            response += " Quale confronto tra i due prodotti ti fa decidere?"
                        else:
                            response += " Vuoi provare a verificare la prossima coppia con lo stesso controllo?"
                    output = {
                        "ok": True,
                        "action": action,
                        "response": response,
                        "source_mode": "deterministic_core",
                        "deterministic": True,
                        "core_evidence": relation,
                        "pedagogy": decision.model_dump(mode="json"),
                    }
                    self.store.event(session_id, action, {
                        "response": response,
                        "request": _bounded_request(payload),
                        "source_mode": "deterministic_core",
                        "strategy": decision.strategy.value,
                        "student_move": student_move,
                        "mode": decision.mode.value,
                        "model_path": "none",
                        "core_tool": "core.fraction_relation",
                    })
                    return output
        evidence = _concept_evidence(
            self.deterministic_core, session.get("topic", "")
        )
        if evidence is None:
            return None
        facts = str(evidence.get("evidence") or "").strip()
        misconception = str(evidence.get("misconceptions") or "").strip()
        if not facts:
            return None
        brevity = _brevity_request(student_text)
        history = _history_context(self.store, session_id)
        previous_response = _last_tutor_response(history)
        targeted_facts, fact_score = _targeted_concept_text(
            facts,
            student_text,
            topic=session.get("topic", ""),
            max_sentences=1 if (brevity or previous_response) else 2,
            previous_response=previous_response,
        )
        targeted_guard, guard_score = _targeted_concept_text(
            misconception, student_text, topic=session.get("topic", ""), max_sentences=1
        ) if misconception else ("", 0)
        claim_check = _claim_like_for_evidence(student_text, student_move)
        supported_open_question = (
            student_move == "question" and fact_score >= 1
        )
        supported_confusion = student_move == "confusion"
        curated_example = _concept_example(facts) if student_move == "request_example" else ""
        supported_example = bool(curated_example)
        if not claim_check and not supported_open_question and not supported_confusion and not supported_example:
            return None

        student = self.store.student(session["student_id"])
        learner = profile_from_student(student)
        effective_move = (
            student_move
            if student_move in {"counterexample", "correction", "confusion", "request_example"}
            else "claim_check" if claim_check else student_move
        )
        if supported_example:
            targeted_facts = curated_example
        decision = select_pedagogy(
            learner,
            action=action,
            subject=session["subject"],
            topic=session["topic"],
            student_move=effective_move,
        )

        if brevity == "one_sentence":
            parts = [targeted_facts]
        elif decision.mode is SessionMode.LITERACY_L2:
            parts = [targeted_facts]
            if decision.micro_check:
                parts.append("Come lo diresti adesso?")
        else:
            if effective_move in {"counterexample", "correction"}:
                lead = "Il punto del tuo esempio è questo."
            elif effective_move == "claim_check":
                lead = "Controlliamo questa idea."
            elif effective_move == "confusion":
                lead = "Ripartiamo da un solo punto sicuro."
            elif effective_move == "request_example":
                lead = "Cambiamo strada con un esempio concreto."
            else:
                lead = "Il punto rilevante è questo."
            parts = [lead, targeted_facts]
            if targeted_guard and guard_score > 0 and effective_move in {"counterexample", "correction", "claim_check"}:
                parts.append(_student_facing_guard(targeted_guard))
            if decision.micro_check:
                if brevity == "short":
                    parts.append("Ti torna?")
                elif effective_move in {"counterexample", "correction", "claim_check"}:
                    parts.append("Qual è la regola corretta in una frase?")
                elif effective_move == "confusion":
                    parts.append("Quale parola o passaggio resta poco chiaro?")
                else:
                    parts.append("Ti torna questo passaggio?")
        response = _trim_response(" ".join(parts), decision.max_response_chars)
        if previous_response and _response_similarity(previous_response, response) >= 0.78:
            if decision.mode is SessionMode.LITERACY_L2:
                response = _trim_response(
                    targeted_facts + " Ripeti solo la forma corretta.",
                    decision.max_response_chars,
                )
            elif effective_move in {"counterexample", "correction"}:
                response = _trim_response(
                    targeted_facts + " Nel tuo esempio, quale dettaglio cambia la conclusione?",
                    decision.max_response_chars,
                )
            elif effective_move == "claim_check":
                response = _trim_response(
                    targeted_facts + " Usa questa verifica sul caso che hai appena proposto.",
                    decision.max_response_chars,
                )
            elif effective_move == "confusion":
                response = _trim_response(
                    targeted_facts + " Dimmi solo quale parola resta poco chiara.",
                    decision.max_response_chars,
                )
            else:
                response = _trim_response(
                    targeted_facts + " Questo risponde al punto nuovo?",
                    decision.max_response_chars,
                )

        output = {
            "ok": True,
            "action": action,
            "response": response,
            "source_mode": "deterministic_core",
            "deterministic": True,
            "core_evidence": {
                "turn_classification": turn_evidence,
                "concept_evidence": evidence,
            },
            "pedagogy": decision.model_dump(mode="json"),
        }
        self.store.event(session_id, action, {
            "response": response,
            "request": _bounded_request(payload),
            "source_mode": "deterministic_core",
            "strategy": decision.strategy.value,
            "student_move": student_move,
            "claim_check": effective_move == "claim_check",
            "mode": decision.mode.value,
            "model_path": "none",
            "core_tool": "core.concept_evidence",
        })
        return output

    def _core_math_hint(
        self,
        session_id: str,
        exercise: str,
        student_attempt: str,
    ) -> dict[str, Any] | None:
        if self.deterministic_core is None:
            return None
        session = self.store.session(session_id)
        scope = f"{session.get('subject', '')} {session.get('topic', '')}".casefold()
        if not any(word in scope for word in ("mat", "aritmet", "algebr", "fraz", "fisic", "chimic")):
            return None
        hint_call = getattr(self.deterministic_core, "math_hint", None)
        if not callable(hint_call):
            return None
        try:
            evidence = hint_call(exercise, student_attempt)
        except Exception:
            return None
        response = str(evidence.get("hint") or "").strip()
        if evidence.get("recognized") is not True or not response:
            return None
        history = _history_context(self.store, session_id)
        previous_response = _last_tutor_response(history)
        if previous_response and _response_similarity(previous_response, response) >= 0.78:
            kind = str(evidence.get("kind") or "")
            if kind == "percent_discount":
                response = (
                    "Cambio strada: calcola prima l'importo dello sconto con la percentuale "
                    "e il prezzo iniziale, poi sottrai quella quantità dal prezzo di partenza. "
                    "Fermati prima del risultato finale."
                )
            elif kind == "linear_equation":
                response = (
                    "Usa il passaggio che hai già ottenuto: se hai kx=c, dividi entrambi i membri "
                    "per lo stesso coefficiente k e fermati prima di scrivere il valore finale di x."
                )
            elif "/" in exercise and any(op in exercise for op in ("+", "-")):
                response = (
                    "Non sommare direttamente sopra e sotto: scegli prima un denominatore comune, "
                    "riscrivi le frazioni in parti della stessa grandezza e fermati prima del risultato."
                )
        _student, _learner, decision = self._decision_for(session, "hint")
        output = {
            "ok": True,
            "action": "hint",
            "response": response,
            "source_mode": "deterministic_core",
            "deterministic": True,
            "core_evidence": evidence,
            "pedagogy": decision.model_dump(mode="json"),
        }
        self.store.event(session_id, "hint", {
            "response": response,
            "request": _bounded_request({
                "exercise": exercise,
                "student_attempt": student_attempt,
            }),
            "source_mode": "deterministic_core",
            "strategy": decision.strategy.value,
            "student_move": "neutral",
            "mode": decision.mode.value,
            "model_path": "none",
            "core_tool": "core.math_hint",
        })
        return output

    def _core_study_plan(
        self,
        session_id: str,
        objective: str,
        available_minutes: int,
    ) -> dict[str, Any] | None:
        if self.deterministic_core is None:
            return None
        session = self.store.session(session_id)
        _student, _learner, decision = self._decision_for(session, "study_plan")
        mode = (
            "literacy_l2" if decision.mode is SessionMode.LITERACY_L2
            else "scholar" if decision.mode is SessionMode.SCHOLAR
            else "standard"
        )
        try:
            plan = self.deterministic_core.study_plan(available_minutes, mode)
        except Exception:
            return None
        blocks = plan.get("blocks")
        if not isinstance(blocks, list) or not blocks:
            return None
        lines = [f"Obiettivo: {objective}"]
        for item in blocks:
            if not isinstance(item, dict):
                return None
            lines.append(f"{item.get('order')}. {item.get('label')} — {item.get('minutes')} min")
        response = "\n".join(lines)
        output = {
            "ok": True, "action": "study_plan", "response": response,
            "source_mode": "deterministic_core", "deterministic": True,
            "plan": plan, "pedagogy": decision.model_dump(mode="json"),
        }
        self.store.event(session_id, "study_plan", {
            "response": response[:4000], "source_mode": "deterministic_core",
            "strategy": decision.strategy.value, "mode": decision.mode.value,
            "model_path": "none", "core_tool": "core.study_plan",
        })
        return output

    @staticmethod
    def _summary_requires_generation(objective: str) -> bool:
        low = objective.casefold()
        return any(word in low for word in (
            "analizza", "analisi", "critica", "confronta", "argomenta",
            "tesi", "tesina", "discuti", "valuta", "sintesi critica",
        ))

    def _core_summary(
        self,
        session_id: str,
        material: str,
        objective: str,
    ) -> dict[str, Any] | None:
        if self.deterministic_core is None or len(material) > 60000:
            return None
        session = self.store.session(session_id)
        _student, _learner, decision = self._decision_for(
            session, "summarize_material", material_supplied=True
        )
        if decision.mode is SessionMode.LITERACY_L2:
            return None
        if self._summary_requires_generation(objective):
            return None
        try:
            evidence = self.deterministic_core.extractive_summary(material, max_sentences=5)
        except Exception:
            return None
        response = str(evidence.get("summary") or "").strip()
        if not response:
            return None
        output = {
            "ok": True, "action": "summarize_material", "response": response,
            "source_mode": "provided_material", "deterministic": True,
            "core_evidence": evidence, "pedagogy": decision.model_dump(mode="json"),
        }
        self.store.event(session_id, "summarize_material", {
            "response": response[:4000],
            "request": _bounded_request({
                "material": material,
                "objective": objective,
                "source_mode": "provided_material",
            }),
            "source_mode": "provided_material",
            "strategy": decision.strategy.value, "mode": decision.mode.value,
            "model_path": "none", "core_tool": "core.extractive_summary",
        })
        return output

    def _deterministic_policy_reply(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any],
        response: str,
        *,
        source_mode: str = "deterministic_policy",
        core_tool: str = "policy.guard",
        student_move: str = "clarification",
    ) -> dict[str, Any]:
        session = self.store.session(session_id)
        _student, _learner, decision = self._decision_for(session, action)
        output = {
            "ok": True,
            "action": action,
            "response": response,
            "source_mode": source_mode,
            "deterministic": True,
            "pedagogy": decision.model_dump(mode="json"),
        }
        self.store.event(session_id, action, {
            "response": response,
            "request": _bounded_request(payload),
            "source_mode": source_mode,
            "strategy": decision.strategy.value,
            "student_move": student_move,
            "mode": decision.mode.value,
            "model_path": "none",
            "core_tool": core_tool,
        })
        return output

    def explain(
        self,
        session_id: str,
        question: str,
        context: str = "",
    ) -> dict[str, Any]:
        payload = {
            "question": question,
            "context": context,
            "instruction": (
                "Spiega il concetto in modo adatto allo studente. "
                "Termina con una breve domanda di verifica."
            ),
        }
        if _needs_reference_clarification(question):
            low_question = " ".join(question.casefold().split())
            clarification = (
                "Scrivi il numero o l'espressione completa, compreso il segno meno, "
                "così posso spiegare proprio quel caso."
                if "meno" in low_question
                else "Quale parola, espressione, azione o passaggio intendi? "
                     "Scrivilo o descrivilo con poche parole, così non devo indovinare."
            )
            return self._deterministic_policy_reply(
                session_id,
                "explain",
                payload,
                clarification,
                core_tool="policy.reference_clarification",
                student_move="question",
            )
        deterministic = self._core_concept_reply(
            session_id, "explain", payload
        )
        if deterministic is not None:
            return deterministic
        return self._teaching_call(
            session_id,
            "explain",
            payload,
        )

    def explain_differently(
        self,
        session_id: str,
        concept: str,
        context: str = "",
    ) -> dict[str, Any]:
        payload = {
            "concept": concept,
            "context": context,
            "instruction": (
                "Rispiega con un approccio diverso, preferendo "
                "un esempio concreto o un'analogia utile."
            ),
        }
        if _needs_reference_clarification(concept):
            return self._deterministic_policy_reply(
                session_id,
                "explain_differently",
                payload,
                "Quale parola, espressione o passaggio intendi? Copialo qui "
                "e lo rispiego senza indovinare il riferimento.",
                core_tool="policy.reference_clarification",
            )
        deterministic = self._core_concept_reply(
            session_id, "explain_differently", payload
        )
        if deterministic is not None:
            return deterministic
        return self._teaching_call(
            session_id,
            "explain_differently",
            payload,
        )

    def hint(
        self,
        session_id: str,
        exercise: str,
        student_attempt: str = "",
    ) -> dict[str, Any]:
        deterministic = self._core_math_hint(
            session_id, exercise, student_attempt
        )
        if deterministic is not None:
            return deterministic
        return self._teaching_call(
            session_id,
            "hint",
            {
                "exercise": exercise,
                "student_attempt": student_attempt,
                "instruction": (
                    "Dai un solo suggerimento progressivo. "
                    "NON fornire la soluzione o il risultato finale."
                ),
            },
        )

    def generate_exercise(
        self,
        session_id: str,
        topic: str = "",
        difficulty: str = "adattiva",
    ) -> dict[str, Any]:
        return self._teaching_call(
            session_id,
            "generate_exercise",
            {
                "topic": topic,
                "difficulty": difficulty,
                "instruction": (
                    "Genera un esercizio appropriato al livello. "
                    "Non includere la soluzione."
                ),
            },
        )

    def check_answer(
        self,
        session_id: str,
        exercise: str,
        student_answer: str,
        show_solution: bool = False,
    ) -> dict[str, Any]:
        result = self._core_math_check(
            session_id, exercise, student_answer, show_solution
        ) or self._teaching_call(
            session_id,
            "check_answer",
            {
                "exercise": exercise,
                "student_answer": student_answer,
                "show_solution": show_solution,
                "instruction": (
                    "Valuta la risposta. Restituisci anche il campo "
                    'booleano "correct". Se è errata, spiega il tipo '
                    "di errore. Se show_solution=false, guida con un "
                    "suggerimento senza rivelare automaticamente la "
                    "soluzione finale."
                ),
            },
        )

        session = self.store.session(session_id)
        correct = result.get("correct")
        if not isinstance(correct, bool):
            correct = None

        self.store.update_progress(
            student_id=session["student_id"],
            subject=session["subject"],
            topic=session["topic"] or "generale",
            correct=correct,
            summary=str(result.get("response") or ""),
        )

        return result

    def quiz(
        self,
        session_id: str,
        topic: str = "",
        questions: int = 5,
    ) -> dict[str, Any]:
        return self._teaching_call(
            session_id,
            "quiz",
            {
                "topic": topic,
                "questions": questions,
                "instruction": (
                    "Prepara una breve interrogazione/quiz. "
                    "Mostra le domande senza le risposte."
                ),
            },
        )

    def study_plan(
        self,
        session_id: str,
        objective: str,
        available_minutes: int = 30,
    ) -> dict[str, Any]:
        return self._core_study_plan(
            session_id, objective, available_minutes
        ) or self._teaching_call(
            session_id,
            "study_plan",
            {
                "objective": objective,
                "available_minutes": available_minutes,
                "instruction": (
                    "Crea un piano di studio concreto e realistico "
                    "per il tempo disponibile."
                ),
            },
        )

    def summarize_material(
        self,
        session_id: str,
        material: str,
        objective: str = "",
    ) -> dict[str, Any]:
        return self._core_summary(
            session_id, material, objective
        ) or self._teaching_call(
            session_id,
            "summarize_material",
            {
                "material": material,
                "objective": objective,
                "source_mode": "provided_material",
                "instruction": (
                    "Riassumi esclusivamente il materiale fornito. "
                    "Non aggiungere fatti esterni."
                ),
            },
        )

    def student_progress(
        self,
        student_id: str,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "student_id": student_id,
            "progress": self.store.progress(student_id),
        }

    def end_session(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        ended = self.store.end_session(session_id)

        release_session = getattr(
            self.model_call,
            "release_session",
            None,
        )

        if callable(release_session):
            release_session(session_id)

        return {
            "ok": True,
            **ended,
        }

    def close(self) -> None:
        closer = getattr(
            self.model_call,
            "close",
            None,
        )

        if callable(closer):
            closer()

    def prepare_reading(
        self,
        session_id: str,
        material: str,
        max_chunk_chars: int = 1200,
    ) -> dict[str, Any]:
        self.store.session(session_id)
        text_profile = None
        effective_chunk_chars = max_chunk_chars
        if self.deterministic_core is not None:
            try:
                text_profile = self.deterministic_core.text_profile(material)
                recommended = int(text_profile.get("recommended_chunk_chars") or max_chunk_chars)
                effective_chunk_chars = min(max_chunk_chars, max(320, recommended))
            except Exception:
                text_profile = None
                effective_chunk_chars = max_chunk_chars

        paragraphs = [
            part.strip()
            for part in material.splitlines()
            if part.strip()
        ]

        chunks: list[str] = []
        current = ""

        for paragraph in paragraphs:
            candidate = (
                paragraph if not current
                else current + "\n" + paragraph
            )
            if len(candidate) <= effective_chunk_chars:
                current = candidate
                continue

            if current:
                chunks.append(current)

            while len(paragraph) > effective_chunk_chars:
                chunks.append(paragraph[:effective_chunk_chars])
                paragraph = paragraph[effective_chunk_chars:]

            current = paragraph

        if current:
            chunks.append(current)

        self.store.event(
            session_id,
            "prepare_reading",
            {"chunks": len(chunks)},
        )

        return {
            "ok": True,
            "source_mode": "provided_material",
            "tts_status": "stub",
            "chunks": chunks,
            "chunk_chars": effective_chunk_chars,
            "text_profile": text_profile,
        }

    def stream_explain(self, session_id: str, question: str, context: str = "") -> Iterator[dict[str, Any]]:
        payload = {
            "question": question,
            "context": context,
            "instruction": "Spiega il concetto in modo adatto allo studente. Se la domanda contiene un'obiezione o un controesempio, affrontalo prima della spiegazione generale. Termina con una breve domanda di verifica.",
        }
        if _needs_reference_clarification(question):
            deterministic = self._deterministic_policy_reply(
                session_id,
                "explain",
                payload,
                "Quale parola, espressione, azione o passaggio intendi? "
                "Scrivilo o descrivilo con poche parole, così non devo indovinare.",
                core_tool="policy.reference_clarification",
            )
            yield {"type": "delta", "text": deterministic["response"]}
            yield {"type": "done", "result": deterministic}
            return
        deterministic = self._core_concept_reply(
            session_id, "explain", payload
        )
        if deterministic is not None:
            yield {"type": "delta", "text": deterministic["response"]}
            yield {"type": "done", "result": deterministic}
            return
        yield from self._stream_teaching_call(
            session_id,
            "explain",
            payload,
        )

    def stream_explain_differently(self, session_id: str, concept: str, context: str = "") -> Iterator[dict[str, Any]]:
        payload = {
            "concept": concept,
            "context": context,
            "instruction": "Rispiega con un approccio diverso. Se lo studente sta contestando una regola, valuta prima il suo controesempio e correggi eventuali semplificazioni; poi usa un esempio concreto o un'analogia utile.",
        }
        if _needs_reference_clarification(concept):
            deterministic = self._deterministic_policy_reply(
                session_id,
                "explain_differently",
                payload,
                "Quale parola, espressione o passaggio intendi? Copialo qui "
                "e lo rispiego senza indovinare il riferimento.",
                core_tool="policy.reference_clarification",
            )
            yield {"type": "delta", "text": deterministic["response"]}
            yield {"type": "done", "result": deterministic}
            return
        deterministic = self._core_concept_reply(
            session_id, "explain_differently", payload
        )
        if deterministic is not None:
            yield {"type": "delta", "text": deterministic["response"]}
            yield {"type": "done", "result": deterministic}
            return
        yield from self._stream_teaching_call(
            session_id,
            "explain_differently",
            payload,
        )

    def stream_hint(self, session_id: str, exercise: str, student_attempt: str = "") -> Iterator[dict[str, Any]]:
        deterministic = self._core_math_hint(
            session_id, exercise, student_attempt
        )
        if deterministic is not None:
            yield {"type": "delta", "text": deterministic["response"]}
            yield {"type": "done", "result": deterministic}
            return
        yield from self._stream_teaching_call(
            session_id,
            "hint",
            {
                "exercise": exercise,
                "student_attempt": student_attempt,
                "instruction": "Dai un solo suggerimento progressivo. NON fornire la soluzione o il risultato finale.",
            },
        )

    def _stream_teaching_call(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        if action not in {"explain", "explain_differently", "hint"}:
            raise ValueError("teacher_stream_action_denied")
        session = self.store.session(session_id)
        student = self.store.student(session["student_id"])
        learner = profile_from_student(student)
        history = _history_context(self.store, session_id)
        grounded_history = _history_has_grounded_source(history)
        student_move, turn_evidence = _student_move(self.deterministic_core, payload)
        decision = select_pedagogy(
            learner,
            action=action,
            subject=session["subject"],
            topic=session["topic"],
            material_supplied=grounded_history,
            show_solution=False,
            student_move=student_move,
        )
        grounded_material = _grounded_source_material(history)
        if (
            action in {"explain", "explain_differently"}
            and grounded_material
            and _grounded_attribution_unsupported(
                _student_turn(payload), grounded_material
            )
        ):
            result = self._deterministic_policy_reply(
                session_id,
                action,
                payload,
                _grounded_abstention_response(history),
                source_mode="provided_material",
                core_tool="policy.grounded_abstention",
            )
            yield {"type": "delta", "text": result["response"]}
            yield {"type": "done", "result": result}
            return
        context = {
            "action": action,
            "student": {
                "school_level": student.get("school_level"),
                "class_year": student.get("class_year"),
                "learner_profile": learner.compact_context(),
            },
            "session": {"subject": session["subject"], "topic": session["topic"]},
            "pedagogy": decision.model_dump(mode="json"),
            "interaction": {"student_move": student_move},
            "request": payload,
        }
        if history:
            context["history"] = history
        deterministic_evidence: dict[str, Any] = {}
        if turn_evidence is not None:
            deterministic_evidence["turn_classification"] = turn_evidence
        concept_evidence = _concept_evidence(self.deterministic_core, session["topic"])
        if concept_evidence is not None:
            deterministic_evidence["concept_evidence"] = concept_evidence
        if deterministic_evidence:
            context["deterministic_evidence"] = deterministic_evidence
        grammar_context = self._grammar_context(session, payload)
        if grammar_context is not None:
            context["grammar_evidence"] = grammar_context
        system_prompt = _teacher_system_prompt(
            decision,
            concept_evidence=concept_evidence,
            grounding_active=grounded_history,
        )
        user_prompt = json.dumps(context, ensure_ascii=False)
        ensure_session = getattr(self.model_call, "ensure_session", None)
        release_session = getattr(self.model_call, "release_session", None)
        if callable(ensure_session):
            ensure_session(session_id)
        final_result: dict[str, Any] | None = None
        draft_chunks: list[str] = []
        try:
            streamer = getattr(self.model_call, "stream", None)
            if callable(streamer):
                events = streamer(system_prompt, user_prompt)
            else:
                raw = self.model_call(system_prompt, user_prompt)
                events = iter((
                    {"type": "delta", "text": str(raw.get("response") or "")},
                    {"type": "done", "result": raw},
                ))
            for event in events:
                if not isinstance(event, dict):
                    raise RuntimeError("teacher_model_invalid_stream_event")
                if event.get("type") == "delta":
                    text = event.get("text")
                    if isinstance(text, str) and text:
                        # Bufferiamo: nessun token del modello viene mostrato prima
                        # della validazione deterministica del contratto d'uscita.
                        draft_chunks.append(text)
                elif event.get("type") == "done" and isinstance(event.get("result"), dict):
                    final_result = event["result"]
                else:
                    raise RuntimeError("teacher_model_invalid_stream_event")
        except Exception:
            if callable(release_session):
                release_session(session_id)
            raise
        if final_result is None:
            raise RuntimeError("teacher_model_stream_missing_done")
        response = str(final_result.get("response") or "").strip()
        if not response and draft_chunks:
            response = "".join(draft_chunks).strip()
        if not response:
            raise RuntimeError("teacher_model_empty_response")
        response, output_guard = _enforce_model_output_contract(
            response,
            decision=decision,
            action=action,
            payload=payload,
            student_move=student_move,
            concept_evidence=concept_evidence,
        )
        output = {
            "ok": True,
            "action": action,
            "response": response,
            "source_mode": (
                "provided_material"
                if grounded_history
                else "general_model_knowledge"
            ),
            "pedagogy": decision.model_dump(mode="json"),
        }
        if output_guard["applied"]:
            output["output_guard"] = output_guard
        self.store.event(
            session_id,
            action,
            {
                "response": response[:4000],
                "request": _bounded_request(payload),
                "source_mode": output["source_mode"],
                "strategy": decision.strategy.value,
                "student_move": student_move,
                "mode": decision.mode.value,
                "model_path": decision.model_path.value,
                "streamed": True,
                "output_guard": output_guard if output_guard["applied"] else None,
            },
        )
        yield {"type": "delta", "text": response}
        yield {"type": "done", "result": output}

    def _teaching_call(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        session = self.store.session(session_id)
        student = self.store.student(session["student_id"])

        learner = profile_from_student(student)
        history = _history_context(self.store, session_id)
        grounded_history = _history_has_grounded_source(history)
        current_material = (
            "material" in payload
            or payload.get("source_mode") == "provided_material"
        )
        grounding_active = bool(current_material or grounded_history)
        student_move, turn_evidence = _student_move(self.deterministic_core, payload)
        decision = select_pedagogy(
            learner,
            action=action,
            subject=session["subject"],
            topic=session["topic"],
            material_supplied=grounding_active,
            show_solution=bool(payload.get("show_solution", False)),
            student_move=student_move,
        )
        grounded_material = _grounded_source_material(history)
        if (
            action in {"explain", "explain_differently"}
            and grounded_material
            and _grounded_attribution_unsupported(
                _student_turn(payload), grounded_material
            )
        ):
            response = _grounded_abstention_response(history)
            output = {
                "ok": True,
                "action": action,
                "response": response,
                "source_mode": "provided_material",
                "deterministic": True,
                "pedagogy": decision.model_dump(mode="json"),
            }
            self.store.event(session_id, action, {
                "response": response,
                "request": _bounded_request(payload),
                "source_mode": "provided_material",
                "strategy": decision.strategy.value,
                "student_move": student_move,
                "mode": decision.mode.value,
                "model_path": "none",
                "core_tool": "policy.grounded_abstention",
            })
            return output
        context = {
            "action": action,
            "student": {
                "school_level": student.get("school_level"),
                "class_year": student.get("class_year"),
                "learner_profile": learner.compact_context(),
            },
            "session": {
                "subject": session["subject"],
                "topic": session["topic"],
            },
            "pedagogy": decision.model_dump(mode="json"),
            "interaction": {"student_move": student_move},
            "request": payload,
        }
        if history:
            context["history"] = history
        deterministic_evidence: dict[str, Any] = {}
        if turn_evidence is not None:
            deterministic_evidence["turn_classification"] = turn_evidence
        concept_evidence = _concept_evidence(self.deterministic_core, session["topic"])
        if concept_evidence is not None:
            deterministic_evidence["concept_evidence"] = concept_evidence
        if deterministic_evidence:
            context["deterministic_evidence"] = deterministic_evidence

        grammar_context = self._grammar_context(session, payload)
        if grammar_context is not None:
            context["grammar_evidence"] = grammar_context

        ensure_session = getattr(
            self.model_call,
            "ensure_session",
            None,
        )
        release_session = getattr(
            self.model_call,
            "release_session",
            None,
        )

        if callable(ensure_session):
            ensure_session(session_id)

        try:
            result = self.model_call(
                _teacher_system_prompt(
                    decision,
                    concept_evidence=concept_evidence,
                    grounding_active=grounding_active,
                ),
                json.dumps(context, ensure_ascii=False),
            )
        except Exception:
            # Un errore del modello non deve lasciare Qwen residente
            # né AgentCPM sospeso indefinitamente.
            if callable(release_session):
                release_session(session_id)
            raise

        if not isinstance(result, dict):
            raise RuntimeError("teacher_model_invalid_result")

        response = str(result.get("response") or "").strip().replace("**", "").replace("__", "")
        if not response:
            raise RuntimeError("teacher_model_empty_response")

        quality_retry: dict[str, Any] | None = None
        previous_response = _last_tutor_response(history)
        comparison_response, _ = _enforce_model_output_contract(
            response,
            decision=decision,
            action=action,
            payload=payload,
            student_move=student_move,
            concept_evidence=concept_evidence,
        )
        initial_similarity = _response_similarity(previous_response, comparison_response)
        if (
            previous_response
            and action in {"explain", "explain_differently"}
            and initial_similarity >= 0.82
        ):
            retry_context = json.loads(json.dumps(context, ensure_ascii=False))
            retry_context["quality_retry"] = {
                "reason": "repetitive_response",
                "previous_response": previous_response[:1800],
                "latest_student_turn": _student_turn(payload)[:1200],
                "instruction": (
                    "Riscrivi da zero. Rispondi al turno corrente, non alla domanda precedente. "
                    "Cambia strategia e non riutilizzare la stessa spiegazione."
                ),
            }
            retry_system = (
                _teacher_system_prompt(
                    decision,
                    concept_evidence=concept_evidence,
                    grounding_active=grounding_active,
                )
                + "\n\nQUALITY RETRY VINCOLANTE:\n"
                + "La bozza precedente era troppo simile alla risposta già data. "
                + "Affronta solo la nuova richiesta e cambia strategia."
            )
            try:
                retry_result = self.model_call(
                    retry_system,
                    json.dumps(retry_context, ensure_ascii=False),
                )
            except Exception:
                retry_result = None
            retry_response = ""
            if isinstance(retry_result, dict):
                retry_response = str(retry_result.get("response") or "").strip().replace("**", "").replace("__", "")
            comparison_retry = retry_response
            if retry_response:
                comparison_retry, _ = _enforce_model_output_contract(
                    retry_response,
                    decision=decision,
                    action=action,
                    payload=payload,
                    student_move=student_move,
                    concept_evidence=concept_evidence,
                )
            retry_similarity = _response_similarity(previous_response, comparison_retry)
            used_fallback = not retry_response or retry_similarity >= 0.90
            response = (
                _repetition_fallback(student_move)
                if used_fallback
                else retry_response
            )
            quality_retry = {
                "reason": "repetitive_response",
                "initial_similarity": round(initial_similarity, 3),
                "retry_similarity": round(retry_similarity, 3),
                "fallback": used_fallback,
            }
            if isinstance(retry_result, dict) and not used_fallback:
                result = retry_result

        response, output_guard = _enforce_model_output_contract(
            response,
            decision=decision,
            action=action,
            payload=payload,
            student_move=student_move,
            concept_evidence=concept_evidence,
        )

        output = {
            "ok": True,
            "action": action,
            "response": response,
            "source_mode": (
                "provided_material"
                if grounding_active
                else "general_model_knowledge"
            ),
            "pedagogy": decision.model_dump(mode="json"),
        }

        if isinstance(result.get("correct"), bool):
            output["correct"] = result["correct"]
        if quality_retry is not None:
            output["quality_retry"] = quality_retry
        if output_guard["applied"]:
            output["output_guard"] = output_guard

        self.store.event(
            session_id,
            action,
            {
                "response": response[:4000],
                "request": _bounded_request(payload),
                "source_mode": output["source_mode"],
                "strategy": decision.strategy.value,
                "student_move": student_move,
                "mode": decision.mode.value,
                "model_path": decision.model_path.value,
                "quality_retry": quality_retry,
                "output_guard": output_guard if output_guard["applied"] else None,
            },
        )

        return output



def _normalize_teacher_model_result(text: str) -> dict[str, Any]:
    content = text.strip()

    if not content:
        raise RuntimeError("teacher_model_empty_response")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Qwen può occasionalmente emettere:
        # response:"testo..."
        # senza le parentesi graffe JSON.
        if content.startswith(("response:", "response=")):
            value = content[len("response:"):].strip()

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {'"', "'"}
            ):
                value = value[1:-1]

            value = value.replace(r'\\"', '"').strip()

            if value:
                return {"response": value.replace("**", "").replace("__", "")}

        return {"response": content.replace("**", "").replace("__", "")}

    if not isinstance(parsed, dict):
        return {"response": content.replace("**", "").replace("__", "")}

    response = parsed.get("response")

    # Alcuni modelli producono correttamente JSON ma annidano il testo
    # pedagogico dentro response. Il contratto Teacher richiede una stringa.
    if isinstance(response, dict):
        parts: list[str] = []

        explanation = response.get("spiegazione")
        if isinstance(explanation, str) and explanation.strip():
            parts.append(explanation.strip())

        question = response.get("domanda")
        if isinstance(question, str) and question.strip():
            parts.append(question.strip())

        if not parts:
            for value in response.values():
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())

        if parts:
            parsed["response"] = "\n\n".join(parts)
        else:
            parsed["response"] = json.dumps(
                response,
                ensure_ascii=False,
            )

    elif isinstance(response, list):
        parts = [
            str(value).strip()
            for value in response
            if str(value).strip()
        ]
        parsed["response"] = "\n".join(parts)

    elif response is not None and not isinstance(response, str):
        parsed["response"] = str(response)

    parsed["response"] = str(parsed.get("response") or "").strip().replace("**", "").replace("__", "")

    if not parsed["response"]:
        raise RuntimeError("teacher_model_empty_response")

    return parsed



class _ResponseStringExtractor:
    """Incrementally expose only the JSON `response` string."""

    def __init__(self) -> None:
        self.buffer = ""
        self.position = 0
        self.started = False
        self.ended = False
        self.escape = False
        self.unicode_digits = ""

    def feed(self, text: str) -> str:
        if self.ended or not text:
            return ""
        self.buffer += text
        if not self.started:
            key = self.buffer.find('"response"')
            if key < 0:
                return ""
            colon = self.buffer.find(":", key + len('"response"'))
            if colon < 0:
                return ""
            quote = self.buffer.find('"', colon + 1)
            if quote < 0:
                return ""
            self.position = quote + 1
            self.started = True

        out: list[str] = []
        while self.position < len(self.buffer) and not self.ended:
            char = self.buffer[self.position]
            self.position += 1
            if self.unicode_digits:
                self.unicode_digits += char
                if len(self.unicode_digits) == 5:
                    try:
                        out.append(chr(int(self.unicode_digits[1:], 16)))
                    except ValueError:
                        out.append("\\" + self.unicode_digits)
                    self.unicode_digits = ""
                continue
            if self.escape:
                self.escape = False
                if char == "u":
                    self.unicode_digits = "u"
                    continue
                out.append({"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f"}.get(char, char))
                continue
            if char == "\\":
                self.escape = True
            elif char == '"':
                self.ended = True
            else:
                out.append(char)
        return "".join(out)


class ScheduledQwenModel:
    """Qwen locale con lease GPU bounded alla sessione Teacher."""

    def __init__(
        self,
        *,
        scheduler: TransactionalGpuScheduler | None = None,
    ) -> None:
        self.base_url = os.getenv(
            "RALF_LLAMA_CPP_BASE_URL",
            "http://127.0.0.1:19091",
        ).rstrip("/")

        parsed_url = urlparse(self.base_url)

        if (
            parsed_url.scheme != "http"
            or parsed_url.hostname not in {
                "127.0.0.1",
                "localhost",
                "::1",
            }
            or parsed_url.port != 19091
        ):
            raise RuntimeError("teacher_model_url_denied")

        self.model = os.getenv(
            "RALF_LLAMA_CPP_MODEL",
            "qwen2.5:7b",
        )
        self.fast_model = os.getenv("RALF_TEACHER_FAST_MODEL", self.model)
        self.deep_model = os.getenv("RALF_TEACHER_DEEP_MODEL", self.model)
        common_options = {
            "temperature": 0.2,
            "cache_prompt": True,
            "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "teacher_response",
                    "strict": True,
                    "schema": TEACHER_RESPONSE_SCHEMA,
                },
            },
        }
        settings = ChatProviderSettings(
            connect_timeout_sec=5.0,
            inactivity_timeout_sec=90.0,
        )
        self.fast_provider = OpenAICompatibleChatProvider(
            base_url=self.base_url,
            model=self.fast_model,
            provider_name="teacher_qwen_fast",
            settings=settings,
            request_options={**common_options, "max_tokens": 768},
        )
        self.deep_provider = OpenAICompatibleChatProvider(
            base_url=self.base_url,
            model=self.deep_model,
            provider_name="teacher_qwen_deep",
            settings=settings,
            request_options={**common_options, "max_tokens": 2048},
        )
        # Historical attribute retained for diagnostics/tests.
        self.provider = self.fast_provider

        self.scheduler = scheduler or TransactionalGpuScheduler()

        self._lease_cm: Any | None = None
        self._lease_session_id: str | None = None
        self._transition: dict[str, Any] | None = None

    def ensure_session(
        self,
        session_id: str,
    ) -> dict[str, Any]:
        if (
            self._lease_cm is not None
            and self._lease_session_id == session_id
        ):
            return {
                "status": "reused",
                "session_id": session_id,
                "transition": self._transition,
            }

        if self._lease_cm is not None:
            self._release_lease()

        cm = self.scheduler.engine_session(
            "qwen_chat",
            task_id=f"teacher:{session_id}",
        )

        transition = cm.__enter__()

        self._lease_cm = cm
        self._lease_session_id = session_id
        self._transition = dict(transition)

        return {
            "status": "started",
            "session_id": session_id,
            "transition": self._transition,
        }

    @staticmethod
    def _strict_system_prompt(system_prompt: str) -> str:
        return (
            system_prompt.rstrip()
            + "\n\n"
            + "CONTRATTO OUTPUT OBBLIGATORIO:\n"
            + "- restituisci un singolo oggetto JSON valido;\n"
            + '- "response" DEVE essere una stringa, mai un oggetto o array;\n'
            + '- per check_answer puoi aggiungere "correct" booleano;\n'
            + "- non usare markdown intorno al JSON."
        )

    def _provider_for(self, user_prompt: str) -> OpenAICompatibleChatProvider:
        try:
            payload = json.loads(user_prompt)
            path = payload.get("pedagogy", {}).get("model_path") if isinstance(payload, dict) else None
        except (TypeError, json.JSONDecodeError):
            path = None
        return self.deep_provider if path == "deep" else self.fast_provider

    def __call__(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        if self._lease_cm is None:
            raise RuntimeError("teacher_model_lease_not_active")

        provider = self._provider_for(user_prompt)
        result = provider.chat([
            {
                "role": "system",
                "content": self._strict_system_prompt(system_prompt),
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ])

        return _normalize_teacher_model_result(result.text)

    def stream(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> Iterator[dict[str, Any]]:
        if self._lease_cm is None:
            raise RuntimeError("teacher_model_lease_not_active")
        provider = self._provider_for(user_prompt)
        extractor = _ResponseStringExtractor()
        raw: list[str] = []
        final_metadata: dict[str, Any] = {}
        messages = [
            {"role": "system", "content": self._strict_system_prompt(system_prompt)},
            {"role": "user", "content": user_prompt},
        ]
        for chunk in provider.stream_chat(messages):
            if chunk.text:
                raw.append(chunk.text)
                delta = extractor.feed(chunk.text)
                if delta:
                    yield {"type": "delta", "text": delta}
            if chunk.done:
                final_metadata = dict(chunk.metadata)
        result = _normalize_teacher_model_result("".join(raw))
        yield {
            "type": "done",
            "result": result,
            "metadata": final_metadata,
            "model_path": "deep" if provider is self.deep_provider else "fast",
        }

    def release_session(
        self,
        session_id: str,
    ) -> None:
        if self._lease_session_id != session_id:
            return

        self._release_lease()

    def _release_lease(self) -> None:
        cm = self._lease_cm

        if cm is None:
            return

        # Azzera prima lo stato locale: close deve essere idempotente
        # anche se il cleanup dello scheduler solleva un errore.
        self._lease_cm = None
        self._lease_session_id = None
        self._transition = None

        cm.__exit__(None, None, None)

    def close(self) -> None:
        self._release_lease()
        seen: set[int] = set()
        for provider in (self.fast_provider, self.deep_provider):
            session = getattr(provider, "session", None)
            if session is None or id(session) in seen:
                continue
            seen.add(id(session))
            closer = getattr(session, "close", None)
            if callable(closer):
                closer()


def scheduled_qwen_model_call(
    system_prompt: str,
    user_prompt: str,
) -> dict[str, Any]:
    """Compatibilità per chiamate standalone fuori da TeacherService."""

    backend = ScheduledQwenModel()

    try:
        backend.ensure_session(
            "teacher-standalone"
        )
        return backend(
            system_prompt,
            user_prompt,
        )
    finally:
        backend.close()


# Nome storico mantenuto per compatibilità.
ollama_model_call = scheduled_qwen_model_call
