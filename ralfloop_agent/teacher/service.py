from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from urllib.error import URLError

from ralfloop_agent.providers.chat import (
    ChatProviderSettings,
    OpenAICompatibleChatProvider,
)
from ralfloop_agent.providers.gpu_engine_scheduler import (
    TransactionalGpuScheduler,
)

from .store import TeacherStore


PEDAGOGY_PROMPT = """
Sei l'insegnante digitale del doposcuola Tiremm Innanz.

Obiettivo: aiutare lo studente a capire e diventare autonomo.

Regole:
- adatta lessico, esempi e difficoltà all'età e alla classe;
- non infantilizzare;
- distingui errori concettuali da errori di calcolo o scrittura;
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
- rispondi nella lingua usata dallo studente salvo richiesta diversa.

Restituisci esclusivamente un oggetto JSON.
Il campo "response" contiene il testo da mostrare allo studente.
"""


ModelCall = Callable[[str, str], dict[str, Any]]


class TeacherService:
    def __init__(
        self,
        store: TeacherStore,
        *,
        model_call: ModelCall | None = None,
    ) -> None:
        self.store = store
        self.model_call = model_call or ScheduledQwenModel()

    def login(
        self,
        card_id: str,
        school_level: str | None = None,
        class_year: str | None = None,
    ) -> dict[str, Any]:
        student = self.store.login(
            card_id,
            school_level=school_level,
            class_year=class_year,
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

    def explain(
        self,
        session_id: str,
        question: str,
    ) -> dict[str, Any]:
        return self._teaching_call(
            session_id,
            "explain",
            {
                "question": question,
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
    ) -> dict[str, Any]:
        return self._teaching_call(
            session_id,
            "explain_differently",
            {
                "concept": concept,
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
        result = self._teaching_call(
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
        return self._teaching_call(
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
        return self._teaching_call(
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
            if len(candidate) <= max_chunk_chars:
                current = candidate
                continue

            if current:
                chunks.append(current)

            while len(paragraph) > max_chunk_chars:
                chunks.append(paragraph[:max_chunk_chars])
                paragraph = paragraph[max_chunk_chars:]

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
        }

    def mindmap_generate(
        self,
        session_id: str,
        material: str,
        objective: str = "",
        max_nodes: int = 12,
    ) -> dict[str, Any]:
        self.store.session(session_id)
        result = self._structured_teaching_call(
            session_id,
            "mindmap_generate",
            {
                "material": material,
                "objective": objective,
                "max_nodes": max_nodes,
                "instruction": (
                    "Crea una mappa mentale modificabile usando solo il materiale fornito. "
                    "Restituisci anche mindmap={title,nodes,edges}; ogni nodo deve avere id,label,summary,importance. "
                    "Massimo max_nodes nodi, testi brevi e adatti anche a uno studente dislessico."
                ),
            },
        )
        mindmap = _normalize_mindmap(result.get("mindmap"), material, max_nodes)
        self.store.event(session_id, "mindmap_generate", {"nodes": len(mindmap["nodes"])})
        return {
            "ok": True,
            "source_mode": "provided_material",
            "mindmap": mindmap,
            "response": str(result.get("response") or "Mappa mentale proposta."),
        }

    def mindmap_update(
        self,
        session_id: str,
        mindmap: dict[str, Any],
        instruction: str,
    ) -> dict[str, Any]:
        self.store.session(session_id)
        current = _normalize_mindmap(mindmap, "", 40)
        result = self._structured_teaching_call(
            session_id,
            "mindmap_update",
            {
                "mindmap": current,
                "instruction": instruction,
                "constraint": (
                    "Modifica solo la struttura fornita secondo la richiesta. "
                    "Non aggiungere fatti esterni. Restituisci mindmap nello stesso schema."
                ),
            },
        )
        updated = _normalize_mindmap(result.get("mindmap") or current, "", 40)
        self.store.event(session_id, "mindmap_update", {"nodes": len(updated["nodes"])})
        return {
            "ok": True,
            "source_mode": "provided_structure",
            "mindmap": updated,
            "response": str(result.get("response") or "Mappa aggiornata."),
        }

    def mindmap_explain(
        self,
        session_id: str,
        mindmap: dict[str, Any],
        node_id: str,
    ) -> dict[str, Any]:
        current = _normalize_mindmap(mindmap, "", 40)
        node = next((item for item in current["nodes"] if item["id"] == node_id), None)
        if node is None:
            raise ValueError("mindmap_node_not_found")
        return self._teaching_call(
            session_id,
            "mindmap_explain",
            {
                "mindmap": current,
                "node": node,
                "instruction": (
                    "Spiega soltanto questo nodo e i collegamenti presenti nella mappa, "
                    "con frasi brevi, un esempio e una domanda finale di verifica."
                ),
            },
        )

    def study_audio_generate(
        self,
        session_id: str,
        material: str,
        mindmap: dict[str, Any] | None = None,
        style: str = "audiobook",
    ) -> dict[str, Any]:
        self.store.session(session_id)
        result = self._structured_teaching_call(
            session_id,
            "study_audio_generate",
            {
                "material": material,
                "mindmap": mindmap,
                "style": style,
                "instruction": (
                    "Trasforma il materiale in uno script audio didattico naturale. "
                    "Usa frasi brevi, segnali verbali di struttura, piccoli richiami e pause logiche. "
                    "Non aggiungere fatti assenti. Restituisci script oltre a response."
                ),
            },
        )
        script = str(result.get("script") or result.get("response") or "").strip()
        if not script:
            raise RuntimeError("teacher_audio_script_empty")
        media = _media_tts_handoff(script)
        self.store.event(session_id, "study_audio_generate", {"media_status": media["status"]})
        return {
            "ok": True,
            "source_mode": "provided_material",
            "script": script,
            "media": media,
            "learning_cycle": {
                "retrieval_prompts": _retrieval_prompts_from_mindmap(mindmap),
                "review_schedule_days": [1, 3, 7, 14],
                "principle": "audio_plus_active_recall_and_spacing",
            },
        }

    def documentary_generate(
        self,
        session_id: str,
        material: str,
        mindmap: dict[str, Any] | None = None,
        duration_minutes: int = 8,
    ) -> dict[str, Any]:
        self.store.session(session_id)
        result = self._structured_teaching_call(
            session_id,
            "documentary_generate",
            {
                "material": material,
                "mindmap": mindmap,
                "duration_minutes": duration_minutes,
                "instruction": (
                    "Scrivi uno script da mini-documentario didattico basato esclusivamente sul materiale. "
                    "Apri con una domanda/contesto, sviluppa per blocchi chiari e chiudi con un riepilogo. "
                    "Restituisci script oltre a response."
                ),
            },
        )
        script = str(result.get("script") or result.get("response") or "").strip()
        if not script:
            raise RuntimeError("teacher_documentary_script_empty")
        media = _media_tts_handoff(script)
        self.store.event(session_id, "documentary_generate", {"media_status": media["status"]})
        return {
            "ok": True,
            "source_mode": "provided_material",
            "script": script,
            "media": media,
            "learning_cycle": {
                "retrieval_prompts": _retrieval_prompts_from_mindmap(mindmap),
                "review_schedule_days": [1, 3, 7, 14],
                "principle": "audio_plus_active_recall_and_spacing",
            },
        }

    def _structured_teaching_call(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        session = self.store.session(session_id)
        student = self.store.student(session["student_id"])
        context = {
            "action": action,
            "student": {
                "school_level": student.get("school_level"),
                "class_year": student.get("class_year"),
                "preferences": student.get("preferences", {}),
            },
            "session": {"subject": session["subject"], "topic": session["topic"]},
            "request": payload,
        }
        ensure_session = getattr(self.model_call, "ensure_session", None)
        release_session = getattr(self.model_call, "release_session", None)
        if callable(ensure_session):
            ensure_session(session_id)
        try:
            result = self.model_call(PEDAGOGY_PROMPT, json.dumps(context, ensure_ascii=False))
        except Exception:
            if callable(release_session):
                release_session(session_id)
            raise
        if not isinstance(result, dict):
            raise RuntimeError("teacher_model_invalid_result")
        return result

    def _teaching_call(
        self,
        session_id: str,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        session = self.store.session(session_id)
        student = self.store.student(session["student_id"])

        context = {
            "action": action,
            "student": {
                "school_level": student.get("school_level"),
                "class_year": student.get("class_year"),
                "preferences": student.get("preferences", {}),
            },
            "session": {
                "subject": session["subject"],
                "topic": session["topic"],
            },
            "request": payload,
        }

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
                PEDAGOGY_PROMPT,
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

        response = str(result.get("response") or "").strip()
        if not response:
            raise RuntimeError("teacher_model_empty_response")

        output = {
            "ok": True,
            "action": action,
            "response": response,
            "source_mode": (
                "provided_material"
                if "material" in payload
                else "general_model_knowledge"
            ),
        }

        if isinstance(result.get("correct"), bool):
            output["correct"] = result["correct"]

        self.store.event(
            session_id,
            action,
            {
                "response": response[:4000],
                "source_mode": output["source_mode"],
            },
        )

        return output



def _normalize_mindmap(value: Any, material: str, max_nodes: int) -> dict[str, Any]:
    if isinstance(value, dict):
        title = str(value.get("title") or "Mappa di studio").strip()[:160] or "Mappa di studio"
        raw_nodes = value.get("nodes") if isinstance(value.get("nodes"), list) else []
        nodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_nodes[:max_nodes]):
            if not isinstance(raw, dict):
                continue
            node_id = str(raw.get("id") or f"n{index + 1}").strip()[:128]
            if not node_id or node_id in seen:
                node_id = f"n{index + 1}"
            seen.add(node_id)
            label = str(raw.get("label") or "Concetto").strip()[:120]
            summary = str(raw.get("summary") or "").strip()[:500]
            importance = str(raw.get("importance") or "medium").strip().lower()
            if importance not in {"low", "medium", "high"}:
                importance = "medium"
            nodes.append({
                "id": node_id,
                "label": label,
                "summary": summary,
                "importance": importance,
            })
        ids = {node["id"] for node in nodes}
        edges: list[dict[str, str]] = []
        raw_edges = value.get("edges") if isinstance(value.get("edges"), list) else []
        for raw in raw_edges:
            if not isinstance(raw, dict):
                continue
            source = str(raw.get("source") or "")
            target = str(raw.get("target") or "")
            if source in ids and target in ids and source != target:
                edges.append({
                    "source": source,
                    "target": target,
                    "label": str(raw.get("label") or "").strip()[:120],
                })
        if nodes:
            return {"title": title, "nodes": nodes, "edges": edges}

    sentences = [
        part.strip()
        for part in material.replace("\n", " ").split(".")
        if part.strip()
    ][:max_nodes]
    nodes = [
        {
            "id": f"n{index + 1}",
            "label": sentence[:80],
            "summary": sentence[:300],
            "importance": "high" if index == 0 else "medium",
        }
        for index, sentence in enumerate(sentences)
    ]
    edges = [
        {"source": "n1", "target": f"n{index + 1}", "label": ""}
        for index in range(1, len(nodes))
    ] if len(nodes) > 1 else []
    return {"title": "Mappa di studio", "nodes": nodes, "edges": edges}


def _retrieval_prompts_from_mindmap(mindmap: dict[str, Any] | None) -> list[str]:
    if not isinstance(mindmap, dict):
        return [
            "Quali sono le tre idee principali che ricordi senza guardare?",
            "Come collegheresti tra loro i concetti principali?",
            "Quale parte sapresti spiegare con parole tue?",
        ]
    normalized = _normalize_mindmap(mindmap, "", 8)
    prompts = [
        f"Senza guardare la mappa, cosa ricordi di: {node['label']}?"
        for node in normalized["nodes"][:3]
    ]
    return prompts or ["Quali sono le idee principali che ricordi senza guardare?"]


def _media_tts_handoff(script: str) -> dict[str, Any]:
    base = os.getenv("RALF_MEDIA_BASE_URL", "http://127.0.0.1:19100").rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.port != 19100:
        return {"status": "denied", "error": "media_url_denied"}
    boundary = "----ralfteacher" + os.urandom(8).hex()
    filename = "tutor-study-audio.txt"
    body = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
    ).encode("utf-8") + script.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode("utf-8")
    import_url = f"{base}/api/v1/audiobooks/import?name={quote('Tutor study audio')}"
    try:
        req = Request(
            import_url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with urlopen(req, timeout=20) as response:
            project = json.loads(response.read().decode("utf-8"))
        project_id = str(project.get("id") or "")
        if not project_id:
            return {"status": "failed", "error": "media_project_missing"}
        payload = json.dumps({
            "project_id": project_id,
            "model": "auto",
            "format": "m4b",
        }).encode("utf-8")
        req = Request(
            f"{base}/api/v1/audiobooks/{quote(project_id)}/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req, timeout=20) as response:
            job = json.loads(response.read().decode("utf-8"))
        return {
            "status": str(job.get("status") or "accepted"),
            "project_id": project_id,
            "job_id": job.get("job_id"),
            "format": "m4b",
        }
    except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
        return {"status": "unavailable", "error": exc.__class__.__name__}


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
        if content.startswith("response:"):
            value = content[len("response:"):].strip()

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {'"', "'"}
            ):
                value = value[1:-1]

            value = value.replace(r'\\"', '"').strip()

            if value:
                return {"response": value}

        return {"response": content}

    if not isinstance(parsed, dict):
        return {"response": content}

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

    if not str(parsed.get("response") or "").strip():
        raise RuntimeError("teacher_model_empty_response")

    return parsed



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

        self.provider = OpenAICompatibleChatProvider(
            base_url=self.base_url,
            model=self.model,
            provider_name="teacher_qwen",
            settings=ChatProviderSettings(
                connect_timeout_sec=5.0,
                inactivity_timeout_sec=90.0,
            ),
            request_options={
                "temperature": 0.2,
                "max_tokens": 512,
                "cache_prompt": True,
                "chat_template_kwargs": {
                    "enable_thinking": False,
                },
            },
        )

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

    def __call__(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> dict[str, Any]:
        if self._lease_cm is None:
            raise RuntimeError("teacher_model_lease_not_active")

        strict_system_prompt = (
            system_prompt.rstrip()
            + "\n\n"
            + "CONTRATTO OUTPUT OBBLIGATORIO:\n"
            + "- restituisci un singolo oggetto JSON valido;\n"
            + '- "response" DEVE essere una stringa, mai un oggetto o array;\n'
            + '- puoi aggiungere solo i campi strutturati esplicitamente richiesti nel request, come mindmap o script;\n'
            + '- per check_answer puoi aggiungere "correct" booleano;\n'
            + "- non usare markdown intorno al JSON."
        )

        result = self.provider.chat([
            {
                "role": "system",
                "content": strict_system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ])

        return _normalize_teacher_model_result(
            result.text
        )

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

        session = getattr(
            self.provider,
            "session",
            None,
        )

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
