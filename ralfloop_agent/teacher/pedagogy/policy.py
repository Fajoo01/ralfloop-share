"""Deterministic pedagogy policy. The LLM executes a decision; it does not invent policy."""
from __future__ import annotations

from enum import StrEnum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from .learner_model import EducationLevel, LanguageLevel, LearnerProfile


class SessionMode(StrEnum):
    MICRO = "micro"
    STANDARD = "standard"
    DOPOSCUOLA = "doposcuola"
    EXAM = "exam"
    SCHOLAR = "scholar"
    LITERACY_L2 = "literacy_l2"


class PedagogyStrategy(StrEnum):
    EXPLICIT_INSTRUCTION = "explicit_instruction"
    WORKED_EXAMPLE = "worked_example"
    FADED_WORKED_EXAMPLE = "faded_worked_example"
    RETRIEVAL_PRACTICE = "retrieval_practice"
    SPACED_PRACTICE = "spaced_practice"
    SELF_EXPLANATION = "self_explanation"
    SOCRATIC = "socratic_questioning"
    TEACH_BACK = "teach_back"
    CONCRETE_REPRESENTATIONAL_ABSTRACT = "concrete_representational_abstract"
    ERROR_ANALYSIS = "error_analysis"
    ORAL_REHEARSAL = "oral_rehearsal"
    VOCABULARY_PREVIEW = "vocabulary_preview"
    ARGUMENT_CRITIQUE = "argument_critique"


class ModelPath(StrEnum):
    FAST = "fast"
    DEEP = "deep"


class AccessPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_first: bool = False
    text_to_speech: bool = False
    speech_to_text: bool = False
    short_lines: bool = False
    line_focus: bool = False
    low_clutter: bool = False
    synchronized_highlight: bool = False
    alternative_response_modes: bool = False
    max_ideas_per_turn: int = Field(default=3, ge=1, le=8)
    response_modes: list[str] = Field(default_factory=lambda: ["text"])


class PedagogyDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: SessionMode
    strategy: PedagogyStrategy
    model_path: ModelPath
    access: AccessPlan
    target_sentences: int = Field(ge=1, le=20)
    max_response_chars: int = Field(ge=120, le=12000)
    micro_check: bool = True
    require_grounding: bool = False
    allow_final_solution: bool = False
    rationale: list[str] = Field(default_factory=list, max_length=8)

    def prompt_contract(self) -> str:
        rules = [
            f"Modalità didattica: {self.mode.value}.",
            f"Strategia: {self.strategy.value}.",
            f"Massimo indicativo: {self.target_sentences} frasi / {self.max_response_chars} caratteri.",
            "Mantieni invariato il livello concettuale: adatta l'accesso, non banalizzare il sapere.",
            "Non diagnosticare DSA, dislessia o altre condizioni cliniche.",
        ]
        if self.access.audio_first:
            rules.append("Audio-first: una consegna alla volta, lessico concreto, risposta orale o scelta quando utile.")
        if self.access.short_lines:
            rules.append("Usa periodi brevi e una sola idea principale per blocco.")
        if self.require_grounding:
            rules.append("Le affermazioni sul materiale devono essere ancorate esclusivamente alle fonti fornite.")
        if not self.allow_final_solution:
            rules.append("Non rivelare la soluzione finale se lo studente può ancora arrivarci con uno scaffolding minimo.")
        if self.strategy is PedagogyStrategy.ERROR_ANALYSIS:
            rules.append("Se lo studente porta un controesempio o contesta una regola, affronta prima quel punto specifico: valuta l’osservazione, correggi eventuali semplificazioni e solo dopo ricollega al concetto.")
        if self.micro_check:
            rules.append("Termina con una micro-verifica appropriata alla modalità di accesso.")
        return "\n".join("- " + rule for rule in rules)


def _is_low_literacy(profile: LearnerProfile) -> bool:
    language = profile.language_profile
    early_reading = language.reading in {LanguageLevel.PRE_A1, LanguageLevel.A1}
    early_writing = language.writing in {LanguageLevel.PRE_A1, LanguageLevel.A1}
    return profile.education_level is EducationLevel.EMERGENT_LITERACY or (
        profile.literacy_profile.access_floor < 0.35 and (early_reading or early_writing)
    )


def _mode(profile: LearnerProfile, action: str, material_supplied: bool) -> SessionMode:
    if profile.session_preference != "auto":
        return SessionMode(profile.session_preference)
    if _is_low_literacy(profile):
        return SessionMode.LITERACY_L2
    if profile.education_level in {EducationLevel.UNIVERSITY, EducationLevel.POSTGRADUATE, EducationLevel.MASTER}:
        return SessionMode.SCHOLAR
    return SessionMode.STANDARD


def _strategy(action: str, recent_errors: Iterable[str], mode: SessionMode, student_move: str = "") -> PedagogyStrategy:
    errors = tuple(recent_errors)
    if student_move in {"counterexample", "correction"} and action in {"explain", "explain_differently"}:
        return PedagogyStrategy.ERROR_ANALYSIS
    if mode is SessionMode.LITERACY_L2:
        return PedagogyStrategy.ORAL_REHEARSAL
    if mode is SessionMode.SCHOLAR and action in {"explain", "summarize_material"}:
        return PedagogyStrategy.ARGUMENT_CRITIQUE
    if action == "hint":
        return PedagogyStrategy.FADED_WORKED_EXAMPLE
    if action == "explain_differently":
        return PedagogyStrategy.CONCRETE_REPRESENTATIONAL_ABSTRACT
    if action == "quiz":
        return PedagogyStrategy.RETRIEVAL_PRACTICE
    if action == "study_plan":
        return PedagogyStrategy.SPACED_PRACTICE
    if action == "check_answer" and any(e in {"conceptual", "missing_prerequisite"} for e in errors):
        return PedagogyStrategy.ERROR_ANALYSIS
    if action == "check_answer":
        return PedagogyStrategy.SELF_EXPLANATION
    if action == "generate_exercise":
        return PedagogyStrategy.WORKED_EXAMPLE
    return PedagogyStrategy.EXPLICIT_INSTRUCTION


def select_pedagogy(
    profile: LearnerProfile,
    *,
    action: str,
    subject: str = "",
    topic: str = "",
    recent_errors: Iterable[str] = (),
    material_supplied: bool = False,
    show_solution: bool = False,
    student_move: str = "",
) -> PedagogyDecision:
    mode = _mode(profile, action, material_supplied)
    support = profile.accessibility_support
    low_literacy = mode is SessionMode.LITERACY_L2
    access = AccessPlan(
        audio_first=low_literacy,
        text_to_speech=support.text_to_speech or low_literacy,
        speech_to_text=support.speech_to_text or low_literacy,
        short_lines=support.short_lines or low_literacy,
        line_focus=support.line_focus,
        low_clutter=support.low_clutter or low_literacy,
        synchronized_highlight=support.synchronized_highlight,
        alternative_response_modes=support.alternative_response_modes or low_literacy,
        max_ideas_per_turn=1 if low_literacy else 2 if support.short_lines else 3,
        response_modes=list(dict.fromkeys(profile.preferred_interaction_modes + (["voice", "choice"] if low_literacy else []))),
    )
    if mode is SessionMode.LITERACY_L2:
        target_sentences, max_chars, model_path = 2, 480, ModelPath.FAST
    elif mode is SessionMode.SCHOLAR:
        target_sentences, max_chars, model_path = 12, 7000, ModelPath.DEEP
    elif support.short_lines or support.line_focus:
        target_sentences, max_chars, model_path = 5, 1200, ModelPath.FAST
    else:
        target_sentences, max_chars, model_path = 7, 2200, ModelPath.FAST
    if action in {"summarize_material", "quiz"} and mode is not SessionMode.LITERACY_L2:
        model_path = ModelPath.DEEP
    rationale = [f"education={profile.education_level.value}", f"mode={mode.value}"]
    if student_move:
        rationale.append(f"student_move={student_move}")
    if low_literacy:
        rationale.append("emergent_reading_access")
    if support.short_lines or support.line_focus:
        rationale.append("reader_support")
    return PedagogyDecision(
        mode=mode,
        strategy=_strategy(action, recent_errors, mode, student_move),
        model_path=model_path,
        access=access,
        target_sentences=target_sentences,
        max_response_chars=max_chars,
        require_grounding=material_supplied,
        allow_final_solution=bool(show_solution),
        micro_check=action not in {"quiz", "study_plan", "summarize_material"},
        rationale=rationale,
    )
