"""Learning orchestration; all resource selection derives from authenticated identity."""
import hashlib
import json
import secrets
import logging

from .audio import BrowserTTS, prepare_tracks
from ..content_pipeline import ContentPipeline, ContentSource
from .learning import ActivityContent, Curriculum, ERRORS, KINDS, choose_activity, evaluate, fraction_evidence, native_content, observe


def _teacher_text(value, limit=4000):
    text = str(value or "").strip()
    for _ in range(3):
        if not text.startswith("{"):
            break
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            break
        if not isinstance(parsed, dict):
            break
        nested = parsed.get("response")
        if nested is None:
            nested = parsed.get("feedback")
        if nested is None:
            break
        text = str(nested).strip()
    return text.replace("**", "").replace("__", "")[:limit]


class LearningApplication:
    def __init__(self, state, teacher, tts=None):
        self.state, self.teacher = state, teacher
        self.curriculum, self.tts = Curriculum(), tts or BrowserTTS()
        self.content_pipeline = ContentPipeline(self.curriculum)

    def home(self, student):
        topics = self.curriculum.available(student)
        progress = self.state.progress(student["id"])
        with self.state.connect() as conn:
            active = conn.execute("SELECT id FROM activities WHERE student=? AND completed IS NULL ORDER BY created DESC LIMIT 1", (student["id"],)).fetchone()
        profile = {k: student[k] for k in ("display_name", "school_level", "grade", "school_track", "demo")}
        profile["learner_profile"] = student.get("learner_profile") or self.state.learner_profile(student)
        return {"profile": profile,
                "topics": topics, "progress": progress, "resume": active[0] if active else None,
                "recommendation": self.next(student, topics[0]["id"]) if topics else None}

    def next(self, student, topic):
        entry = self.curriculum.require(student, topic)
        if topic == "academic_study":
            return {"topic": topic, "activity_type": "guided_exercise", "reason": "scholar_mode", "difficulty": 5}
        if topic.startswith("it_"):
            state = self.state.states(student["id"]).get(topic, {})
            score = state.get("score", 0)
            kind = "guided_exercise" if score < 50 else "free_answer" if score < 80 else "multiple_choice"
            return {"topic": topic, "activity_type": kind, "reason": "structured_literacy", "difficulty": min(5, max(1, 1 + score // 25))}
        result = choose_activity(entry, self.state.states(student["id"]), self.state.recent(student["id"], topic), self.state.clock())
        allowed = list(entry.get("suggested_activity_types") or KINDS)
        if result["activity_type"] == "simulation" and result["topic"] not in ("fractions", "motion"):
            result["activity_type"] = "guided_exercise"
        if result["activity_type"] not in allowed:
            preferred = ["guided_exercise", "multiple_choice", "free_answer", "matching", "true_false"]
            result["activity_type"] = next((kind for kind in preferred if kind in allowed), allowed[0])
        return result

    def public_activity(self, row):
        content = json.loads(row["content"])
        # Hidden answers only appear when flipping ungraded flashcards; never in scored requests.
        answer = content.pop("answer")
        if row["kind"] == "flashcards":
            content["backs"] = answer
        return {"activity_id": row["id"], "topic": row["topic"], "difficulty": row["difficulty"],
                "source": row["source"], "reason": row["reason"], "completed": row["completed"] is not None,
                "learning_objective": self.curriculum.topics[row["topic"]]["learning_objectives"][0],
                "simulation": json.loads(row["simulation"]) if row["simulation"] else None, **content}

    def generate(self, student, topic, kind=None, material_id=None):
        selection = self.next(student, topic)
        topic = selection["topic"]
        entry = self.curriculum.require(student, topic)
        kind = kind or selection["activity_type"]
        allowed_kinds = set(entry.get("suggested_activity_types") or KINDS)
        if kind not in KINDS or kind not in allowed_kinds or (kind == "simulation" and topic not in ("fractions", "motion")):
            raise ValueError("unsupported_activity")
        material = self.state.owned("materials", student["id"], material_id) if material_id else None
        if material:
            with self.state.connect() as conn:
                mapping = conn.execute("SELECT 1 FROM material_mapping WHERE material=? AND topic=?", (material_id, topic)).fetchone()
            if not mapping:
                raise ValueError("material_topic_mismatch")
        with self.state.connect() as conn:
            # Resume protects against refresh farming and redundant model calls.
            rows = conn.execute("SELECT * FROM activities WHERE student=? AND topic=? AND kind=? AND completed IS NULL AND material IS ? ORDER BY created DESC", (student["id"], topic, kind, material_id)).fetchall()
            for row in rows:
                attempts = conn.execute("SELECT COUNT(*) FROM attempts WHERE activity=?", (row["id"],)).fetchone()[0]
                if attempts < 5:
                    return self.public_activity(dict(row))
            variant = conn.execute("SELECT COUNT(*) FROM activities WHERE student=? AND topic=?", (student["id"], topic)).fetchone()[0]
        content = native_content(topic, kind, variant)
        source = "original_curriculum_content"
        if kind in ("free_answer", "guided_exercise", "multiple_choice", "fill_blank"):
            template = {"activity_type": kind, "instructions": "Consegna dell'esercizio", "items": [], "choices": [], "answer": "", "hints": ["Un suggerimento"], "evaluation_rule": "semantic"}
            if kind == "multiple_choice":
                template.update(choices=["A", "B", "C"], answer="A", evaluation_rule="exact")
            instruction = f"{entry['title']}. Obiettivo: {entry['learning_objectives'][0]}. Il valore del campo response deve essere SOLO una stringa JSON valida con questa struttura, sostituendo i testi di esempio: " + json.dumps(template, ensure_ascii=False) + ". Non aggiungere un campo response dentro questa struttura. Niente Markdown, HTML o codice."
            if kind != "multiple_choice":
                # The established Teacher contract is a structured envelope with a text
                # response. The application supplies the renderer/evaluation schema;
                # no incompatible nested JSON contract is forced on production.
                instruction = f"{entry['title']}. Obiettivo: {entry['learning_objectives'][0]}. Modalità: {kind}. Genera un solo esercizio breve. Il campo response contiene la consegna in testo semplice, senza soluzione, HTML o blocchi di codice."
            if material:
                instruction = f"Usa esclusivamente questo estratto autorizzato: {material['text'][:350]}. " + instruction
            try:
                for attempt in range(2):
                    output = self.teacher.perform(student, entry, "teacher.generate_exercise", {"topic": instruction[:1000], "difficulty": str(selection["difficulty"])})
                    try:
                        response = output["response"]
                        if kind != "multiple_choice" and isinstance(response, str) and not response.lstrip().startswith(("{", "[", "```")):
                            candidate = ActivityContent(activity_type=kind, instructions=response, evaluation_rule="semantic")
                        else:
                            candidate = ActivityContent.model_validate(json.loads(response))
                        break
                    except (ValueError, TypeError, KeyError):
                        if attempt: raise
                        instruction = "Correggi il formato: deve essere JSON valido, senza chiavi extra. " + instruction
                if candidate.activity_type != kind:
                    raise ValueError("unexpected_activity_type")
                content, source = candidate, "teacher_material" if material else "teacher_generated"
            except Exception as exc:
                logging.getLogger("teacher.web").warning("activity_generation_fallback student=%s activity_type=%s error_class=%s", student["id"][:12], kind, type(exc).__name__)
                source = "original_fallback"
                # Never misrepresent generic fallback as deriving from a student's book.
                if material:
                    raise ConnectionError("material_generation_unavailable")
        if material and source == "original_curriculum_content":
            raise ValueError("material_generation_requires_exercise")
        raw = content.model_dump_json()
        canonical = content.model_dump(exclude={"activity_type", "hints"})
        fingerprint = hashlib.sha256((topic + json.dumps(canonical, sort_keys=True)).encode()).hexdigest()
        activity_id = secrets.token_hex(16)
        with self.state.connect() as conn:
            conn.execute("INSERT INTO activities(id,student,topic,kind,content,fingerprint,source,reason,difficulty,created,material) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (activity_id, student["id"], topic, kind, raw, fingerprint, source, selection["reason"], selection["difficulty"], self.state.clock(), material_id))
        return self.public_activity(self.state.owned("activities", student["id"], activity_id))

    def answer(self, student, activity_id, answer, key):
        row = self.state.owned("activities", student["id"], activity_id)
        with self.state.connect() as conn:
            old = conn.execute("SELECT activity,result FROM attempts WHERE student=? AND request_key=?", (student["id"], key)).fetchone()
            if old:
                if old["activity"] != activity_id: raise ValueError("idempotency_conflict")
                return json.loads(old["result"])
            if row["completed"] is not None:
                previous = conn.execute("SELECT result FROM attempts WHERE activity=? ORDER BY id DESC LIMIT 1", (activity_id,)).fetchone()
                return {**json.loads(previous[0]), "xp_awarded": 0, "already_completed": True}
            if conn.execute("SELECT COUNT(*) FROM attempts WHERE activity=?", (activity_id,)).fetchone()[0] >= 5:
                raise ValueError("attempt_limit")
        content = json.loads(row["content"])
        verified_math = fraction_evidence(content["instructions"], answer) if row["topic"] == "fractions" and row["kind"] != "simulation" else None
        if row["kind"] == "flashcards":
            raise ValueError("flashcards_are_ungraded_choose_quiz")
        error, feedback = None, "Bene! Hai collegato correttamente i concetti."
        if content["evaluation_rule"] in ("semantic", "reflection"):
            simulation = json.loads(row["simulation"]) if row["simulation"] else None
            if content["evaluation_rule"] == "reflection" and (not simulation or "observation" not in simulation):
                raise ValueError("prediction_and_observation_required")
            prompt = content["instructions"]
            if simulation:
                prompt += " Dati osservati: " + json.dumps(simulation)
            prompt += " Se errata, inizia il testo response con un solo codice tra [distraction], [calculation], [conceptual], [missing_prerequisite], [instruction], [incomplete], [nearly_correct], poi feedback. Non annidare JSON nel testo. Restituisci correct booleano."
            output = self.teacher.perform(student, self.curriculum.topics[row["topic"]], "teacher.check_answer", {"exercise": prompt[:12000], "student_answer": answer if isinstance(answer, str) else json.dumps(answer), "show_solution": False})
            if type(output.get("correct")) is not bool and verified_math is None:
                raise ConnectionError("invalid_teacher_evaluation")
            correct, feedback = output.get("correct", False), _teacher_text(output.get("response"), 2000)
            if not correct:
                error = "conceptual"
                for category in ERRORS:
                    if feedback.startswith("[" + category + "]"):
                        error, feedback = category, feedback[len(category) + 2:].strip()
                        break
            try:
                parsed = json.loads(feedback)
                feedback = str(parsed["feedback"])[:2000]
                if not correct and parsed.get("error_type") in ERRORS:
                    error = parsed["error_type"]
            except (ValueError, TypeError, KeyError):
                pass
            if verified_math is not None:
                correct, error, feedback = verified_math["correct"], verified_math["error_type"], verified_math["feedback"]
        else:
            correct = evaluate(content, answer)
            if not correct:
                error = "incomplete" if not answer else "conceptual" if content["evaluation_rule"] == "pairs" else "calculation" if row["topic"] in ("fractions", "motion") else "nearly_correct"
                feedback = content["hints"][0] if content["hints"] else "Rileggi la consegna e prova un altro collegamento."
        result = self.state.record_answer(student["id"], activity_id, key, correct, error, feedback)
        result["next"] = self.next(student, row["topic"])
        return result

    def simulate(self, student, activity_id, prediction=None, variables=None):
        row = self.state.owned("activities", student["id"], activity_id)
        if row["kind"] != "simulation" or row["completed"]:
            raise ValueError("simulation_unavailable")
        with self.state.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("SELECT simulation FROM activities WHERE id=?", (activity_id,)).fetchone()[0]
            flow = json.loads(current) if current else {}
            if prediction is not None:
                if flow:
                    raise ValueError("prediction_already_recorded")
                flow = {"prediction": prediction}
            elif variables is not None:
                if not flow.get("prediction"):
                    raise ValueError("prediction_required")
                flow.update(variables=variables, observation=observe(row["topic"], variables))
            else:
                raise ValueError("simulation_input_required")
            conn.execute("UPDATE activities SET simulation=? WHERE id=?", (json.dumps(flow), activity_id))
        return flow

    def _help_request(self, student, activity_id, mode, question):
        row = self.state.owned("activities", student["id"], activity_id)
        content, entry = json.loads(row["content"]), self.curriculum.topics[row["topic"]]
        if mode == "hint":
            name, args = "teacher.hint", {"exercise": content["instructions"], "student_attempt": question}
        elif mode == "different":
            name, args = "teacher.explain_differently", {"concept": content["instructions"] + " " + question}
        else:
            name, args = "teacher.explain", {"question": content["instructions"] + " " + question}
        fallback = content["hints"][0] if content["hints"] else "Rileggi la consegna, un passaggio alla volta."
        return entry, name, args, fallback

    def help(self, student, activity_id, mode, question):
        entry, name, args, fallback = self._help_request(student, activity_id, mode, question)
        try:
            result = self.teacher.perform(student, entry, name, args)
            return {"feedback": _teacher_text(result.get("response"), 4000), "source": "teacher",
                    "pedagogy": result.get("pedagogy")}
        except Exception:
            return {"feedback": fallback, "source": "original_fallback"}

    def help_stream(self, student, activity_id, mode, question):
        entry, name, args, fallback = self._help_request(student, activity_id, mode, question)
        streamer = getattr(self.teacher, "stream_perform", None)
        if not callable(streamer):
            result = self.help(student, activity_id, mode, question)
            yield {"type": "delta", "text": result["feedback"]}
            yield {"type": "done", "result": result}
            return
        try:
            final = None
            for event in streamer(student, entry, name, args):
                if not isinstance(event, dict):
                    raise ValueError("invalid_teacher_stream_event")
                if event.get("type") == "delta" and isinstance(event.get("text"), str):
                    yield event
                elif event.get("type") == "done" and isinstance(event.get("result"), dict):
                    final = event["result"]
            if final is None:
                raise ValueError("teacher_stream_missing_done")
            result = {
                "feedback": _teacher_text(final.get("response"), 4000),
                "source": "teacher",
                "pedagogy": final.get("pedagogy"),
            }
            yield {"type": "done", "result": result}
        except Exception:
            yield {"type": "delta", "text": fallback}
            yield {"type": "done", "result": {"feedback": fallback, "source": "original_fallback"}}

    def add_material(self, student, title, text, rights, kind="notes", chapter="", pages=""):
        if rights not in ("own", "authorized", "public_domain", "compatible_license"):
            raise ValueError("material_rights_required")
        material = secrets.token_hex(16)
        record = self.content_pipeline.ingest(student, ContentSource(
            source_id=material,
            title=title,
            text=text,
            source_class="student_material",
            rights=rights,
            license={
                "own": "user_owned",
                "authorized": "user_authorized",
                "public_domain": "public_domain",
                "compatible_license": "compatible_license",
            }[rights],
            locator=(chapter + (" · " + pages if pages else "")).strip(" ·"),
        ))
        topics = record.topic_ids
        with self.state.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM materials WHERE student=?", (student["id"],)).fetchone()[0]
            if count >= 30: raise ValueError("material_limit")
            conn.execute("INSERT INTO materials VALUES(?,?,?,?,?,?,?,?,?)", (material, student["id"], title, kind, chapter, pages, text, rights, self.state.clock()))
            for topic in topics:
                conn.execute("INSERT INTO material_mapping VALUES(?,?)", (material, topic))
        return {"material_id": material, "topics": topics, "content_hash": record.content_hash, "trusted": record.trusted}

    def materials(self, student):
        with self.state.connect() as conn:
            rows = conn.execute("SELECT id,title,kind,chapter,pages,rights FROM materials WHERE student=? ORDER BY created DESC", (student["id"],)).fetchall()
            return [{**dict(r), "topics": [t[0] for t in conn.execute("SELECT topic FROM material_mapping WHERE material=?", (r["id"],))]} for r in rows]

    def material_action(self, student, material_id, action):
        material = self.state.owned("materials", student["id"], material_id)
        topics = self.curriculum.map_material(student, material["text"])
        if not topics: raise ValueError("material_needs_curriculum_mapping")
        entry = self.curriculum.topics[topics[0]]
        if action == "exercise":
            return self.generate(student, topics[0], "free_answer", material_id)
        if action == "quiz":
            # Existing quiz tool used as pedagogical preparation, then a validated scored activity.
            self.teacher.perform(student, entry, "teacher.quiz", {"topic": ("Solo da questo estratto: " + material["text"])[:1000], "questions": 1})
            return self.generate(student, topics[0], "multiple_choice", material_id)
        if action == "audio":
            with self.state.connect() as conn:
                previous = conn.execute("SELECT id,provider,tracks FROM audio_assets WHERE student=? AND material=? AND provider=? ORDER BY created DESC LIMIT 1", (student["id"], material_id, self.tts.name)).fetchone()
            if previous:
                return {"audio_id": previous["id"], "provider": previous["provider"], "tracks": json.loads(previous["tracks"])}
            prepared = self.teacher.perform(student, entry, "teacher.prepare_reading", {"material": material["text"], "max_chunk_chars": 600})
            chunks = prepared.get("chunks")
            if not isinstance(chunks, list) or not chunks or any(not isinstance(x, str) for x in chunks) or sum(map(len, chunks)) > 20000:
                raise ValueError("invalid_reading_output")
            tracks = prepare_tracks(chunks, self.tts)
            asset = secrets.token_hex(16)
            with self.state.connect() as conn:
                conn.execute("INSERT INTO audio_assets(id,student,material,provider,tracks,created) VALUES(?,?,?,?,?,?)", (asset, student["id"], material_id, self.tts.name, json.dumps(tracks), self.state.clock()))
            return {"audio_id": asset, "provider": self.tts.name, "tracks": tracks}
        name = "teacher.summarize_material"
        objective = entry["learning_objectives"][0]
        if action == "explain":
            objective = "Spiega il contenuto con intuizione, formalizzazione, esempio, limiti e una domanda di verifica, senza aggiungere fatti esterni."
        args = {"material": material["text"], "objective": objective}
        out = self.teacher.perform(student, entry, name, args)
        return {
            "feedback": _teacher_text(out.get("response"), 7000),
            "source": "provided_material",
            "pedagogy": out.get("pedagogy"),
            "source_ref": {"title": material["title"], "chapter": material["chapter"], "pages": material["pages"]},
        }

    def plan(self, student, minutes):
        states = self.state.states(student["id"])
        topics = sorted(self.curriculum.available(student), key=lambda t: states.get(t["id"], {}).get("score", 0))[:max(1, min(4, minutes // 5))]
        if not topics: raise ValueError("curriculum_coverage_pending")
        try:
            out = self.teacher.perform(student, topics[0], "teacher.study_plan", {"objective": "Ripassa questi obiettivi: " + "; ".join(t["learning_objectives"][0] for t in topics), "available_minutes": minutes})
            explanation = _teacher_text(out.get("response"), 2000)
        except Exception:
            explanation = "Inizia dagli argomenti da consolidare, poi verifica ciò che hai imparato."
        with self.state.connect() as conn:
            conn.execute("INSERT INTO study_plans VALUES(?,?,?,?) ON CONFLICT(student) DO UPDATE SET topics=excluded.topics,minutes=excluded.minutes,updated=excluded.updated", (student["id"], json.dumps([t["id"] for t in topics]), minutes, self.state.clock()))
        return {"explanation": explanation, "activities": [self.next(student, t["id"]) for t in topics]}
