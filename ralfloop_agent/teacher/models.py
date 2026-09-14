from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .pedagogy import LearnerProfile


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginInput(StrictModel):
    card_id: str = Field(min_length=1, max_length=256)
    school_level: str | None = Field(default=None, max_length=80)
    class_year: str | None = Field(default=None, max_length=80)
    learner_profile: LearnerProfile | None = None


class StartSessionInput(StrictModel):
    student_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=120)
    topic: str = Field(default="", max_length=300)


class ExplainInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=12000)


class ExplainDifferentlyInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    concept: str = Field(min_length=1, max_length=12000)


class HintInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    exercise: str = Field(min_length=1, max_length=12000)
    student_attempt: str = Field(default="", max_length=12000)


class GenerateExerciseInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    topic: str = Field(default="", max_length=1000)
    difficulty: str = Field(default="adattiva", max_length=80)


class CheckAnswerInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    exercise: str = Field(min_length=1, max_length=12000)
    student_answer: str = Field(min_length=1, max_length=12000)
    show_solution: bool = False


class QuizInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    topic: str = Field(default="", max_length=1000)
    questions: int = Field(default=5, ge=1, le=20)


class StudyPlanInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    objective: str = Field(min_length=1, max_length=4000)
    available_minutes: int = Field(default=30, ge=5, le=480)


class SummarizeMaterialInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    material: str = Field(min_length=1, max_length=100000)
    objective: str = Field(default="", max_length=2000)


class StudentProgressInput(StrictModel):
    student_id: str = Field(min_length=1, max_length=128)


class EndSessionInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)


class PrepareReadingInput(StrictModel):
    session_id: str = Field(min_length=1, max_length=128)
    material: str = Field(min_length=1, max_length=100000)
    max_chunk_chars: int = Field(default=1200, ge=200, le=5000)
