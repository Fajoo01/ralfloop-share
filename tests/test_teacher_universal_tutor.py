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
    session = service.start_session(student["student_id"], "italiano L2", "azioni quotidiane")["session"]
    result = service.explain(session["session_id"], "Come si dice questa azione?")

    assert result["pedagogy"]["mode"] == "literacy_l2"
    assert captured["payload"]["student"]["learner_profile"]["language_profile"]["reading"] == "pre-A1"
    assert captured["payload"]["pedagogy"]["access"]["audio_first"] is True
    assert "DECISIONE PEDAGOGICA DETERMINISTICA" in captured["system"]

    persisted = service.login("CARD-L2")["student"]
    assert persisted["preferences"]["learner_profile"]["education_level"] == "emergent_literacy"


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
