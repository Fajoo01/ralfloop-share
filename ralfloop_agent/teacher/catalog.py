from __future__ import annotations

from .models import (
    CheckAnswerInput,
    EndSessionInput,
    ExplainDifferentlyInput,
    ExplainInput,
    GenerateExerciseInput,
    HintInput,
    LoginInput,
    PrepareReadingInput,
    MindMapGenerateInput,
    MindMapUpdateInput,
    MindMapExplainInput,
    StudyAudioGenerateInput,
    DocumentaryGenerateInput,
    QuizInput,
    StartSessionInput,
    StudentProgressInput,
    StudyPlanInput,
    SummarizeMaterialInput,
)
from src.teacher import (
    ALL_TOOLS,
    CHECK_ANSWER,
    END_SESSION,
    EXPLAIN,
    EXPLAIN_DIFFERENTLY,
    GENERATE_EXERCISE,
    HINT,
    LOGIN,
    PREPARE_READING,
    MINDMAP_GENERATE,
    MINDMAP_UPDATE,
    MINDMAP_EXPLAIN,
    STUDY_AUDIO_GENERATE,
    DOCUMENTARY_GENERATE,
    QUIZ,
    START_SESSION,
    STUDENT_PROGRESS,
    STUDY_PLAN,
    SUMMARIZE_MATERIAL,
)


TOOL_MODELS = {
    LOGIN: LoginInput,
    START_SESSION: StartSessionInput,
    EXPLAIN: ExplainInput,
    EXPLAIN_DIFFERENTLY: ExplainDifferentlyInput,
    HINT: HintInput,
    GENERATE_EXERCISE: GenerateExerciseInput,
    CHECK_ANSWER: CheckAnswerInput,
    QUIZ: QuizInput,
    STUDY_PLAN: StudyPlanInput,
    SUMMARIZE_MATERIAL: SummarizeMaterialInput,
    STUDENT_PROGRESS: StudentProgressInput,
    END_SESSION: EndSessionInput,
    PREPARE_READING: PrepareReadingInput,
    MINDMAP_GENERATE: MindMapGenerateInput,
    MINDMAP_UPDATE: MindMapUpdateInput,
    MINDMAP_EXPLAIN: MindMapExplainInput,
    STUDY_AUDIO_GENERATE: StudyAudioGenerateInput,
    DOCUMENTARY_GENERATE: DocumentaryGenerateInput,
}


DESCRIPTIONS = {
    LOGIN:
        "Resolve a Tiremm Innanz student card into an internal student profile.",
    START_SESSION:
        "Start a teaching session for a student, subject and topic.",
    EXPLAIN:
        "Explain a concept at the student's school level.",
    EXPLAIN_DIFFERENTLY:
        "Explain the same concept again using a different approach.",
    HINT:
        "Give one progressive hint without revealing the final solution.",
    GENERATE_EXERCISE:
        "Generate an exercise adapted to the student.",
    CHECK_ANSWER:
        "Check a student's answer and guide correction.",
    QUIZ:
        "Generate a short quiz or oral-exam style interrogation.",
    STUDY_PLAN:
        "Create a bounded study plan.",
    SUMMARIZE_MATERIAL:
        "Summarize only the supplied study material.",
    STUDENT_PROGRESS:
        "Read the student's persisted learning progress.",
    END_SESSION:
        "End an active teaching session.",
    PREPARE_READING:
        "Prepare supplied material for future TTS/audiobook reading.",
    MINDMAP_GENERATE:
        "Generate a bounded editable mind map only from supplied study material.",
    MINDMAP_UPDATE:
        "Revise an existing mind map according to the student's instruction.",
    MINDMAP_EXPLAIN:
        "Explain one selected node of an existing mind map.",
    STUDY_AUDIO_GENERATE:
        "Create a dyslexia-friendly narrated study script and bounded audio handoff.",
    DOCUMENTARY_GENERATE:
        "Create a short educational documentary script and bounded audio handoff.",
}


if tuple(TOOL_MODELS) != ALL_TOOLS:
    raise RuntimeError("teacher_tool_catalog_mismatch")

if set(DESCRIPTIONS) != set(ALL_TOOLS):
    raise RuntimeError("teacher_description_catalog_mismatch")


__all__ = ["DESCRIPTIONS", "TOOL_MODELS"]
