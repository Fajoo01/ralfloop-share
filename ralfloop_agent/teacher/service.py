from __future__ import annotations

import json
import os
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
    budget = 9000
    selected: list[dict[str, Any]] = []
    try:
        events = store.recent_events(session_id, limit=8)
    except Exception:
        return []
    for event in reversed(events):
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        request = payload.get("request")
        item: dict[str, Any] = {
            "action": str(event.get("kind") or ""),
        }
        if isinstance(request, dict) and request:
            item["student_request"] = request
        response = payload.get("response")
        if isinstance(response, str) and response.strip():
            item["tutor_response"] = response.strip()[:3000]
        source_mode = payload.get("source_mode")
        if isinstance(source_mode, str) and source_mode:
            item["source_mode"] = source_mode
        if len(item) == 1:
            continue
        encoded = json.dumps(item, ensure_ascii=False)
        if len(encoded) > budget and selected:
            break
        if len(encoded) > budget:
            if "tutor_response" in item:
                item["tutor_response"] = item["tutor_response"][:1200]
            request_value = item.get("student_request")
            if isinstance(request_value, dict) and isinstance(request_value.get("material"), str):
                request_value = dict(request_value)
                request_value["material"] = request_value["material"][:3500]
                item["student_request"] = request_value
            encoded = json.dumps(item, ensure_ascii=False)
        if len(encoded) > budget:
            continue
        selected.append(item)
        budget -= len(encoded)
    selected.reverse()
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
        if correct:
            response = "Corretto: la risposta è numericamente equivalente al risultato dell'espressione."
        elif show_solution:
            response = f"La risposta non è corretta. Il risultato dell'espressione è {evidence.get('expected')}."
        else:
            response = "La risposta non è corretta. Ricontrolla segni, ordine delle operazioni e semplificazione, senza cambiare il risultato a caso."
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
        if decision.mode in {SessionMode.LITERACY_L2, SessionMode.SCHOLAR}:
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

    def explain(
        self,
        session_id: str,
        question: str,
        context: str = "",
    ) -> dict[str, Any]:
        return self._teaching_call(
            session_id,
            "explain",
            {
                "question": question,
                "context": context,
                "instruction": (
                    "Spiega il concetto in modo adatto allo studente. "
                    "Termina con una breve domanda di verifica."
                ),
            },
        )

    def explain_differently(
        self,
        session_id: str,
        concept: str,
        context: str = "",
    ) -> dict[str, Any]:
        return self._teaching_call(
            session_id,
            "explain_differently",
            {
                "concept": concept,
                "context": context,
                "instruction": (
                    "Rispiega con un approccio diverso, preferendo "
                    "un esempio concreto o un'analogia utile."
                ),
            },
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
        yield from self._stream_teaching_call(
            session_id,
            "explain",
            {
                "question": question,
                "context": context,
                "instruction": "Spiega il concetto in modo adatto allo studente. Se la domanda contiene un'obiezione o un controesempio, affrontalo prima della spiegazione generale. Termina con una breve domanda di verifica.",
            },
        )

    def stream_explain_differently(self, session_id: str, concept: str, context: str = "") -> Iterator[dict[str, Any]]:
        yield from self._stream_teaching_call(
            session_id,
            "explain_differently",
            {
                "concept": concept,
                "context": context,
                "instruction": "Rispiega con un approccio diverso. Se lo studente sta contestando una regola, valuta prima il suo controesempio e correggi eventuali semplificazioni; poi usa un esempio concreto o un'analogia utile.",
            },
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
                        yield {"type": "delta", "text": text}
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
        response = str(final_result.get("response") or "").strip().replace("**", "").replace("__", "")
        if not response:
            raise RuntimeError("teacher_model_empty_response")
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
            },
        )
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
