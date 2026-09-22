"""Validated learner model for adaptation without clinical diagnosis."""
from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class LanguageLevel(StrEnum):
    PRE_A1 = "pre-A1"
    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C1 = "C1"
    C2 = "C2"
    UNKNOWN = "unknown"


class EducationLevel(StrEnum):
    EMERGENT_LITERACY = "emergent_literacy"
    PRIMARY = "primary"
    MIDDLE = "middle"
    UPPER = "upper"
    UNIVERSITY = "university"
    POSTGRADUATE = "postgraduate"
    MASTER = "master"
    OTHER = "other"


class LanguageProfile(StrictModel):
    l1: list[str] = Field(default_factory=list, max_length=4)
    l2: str | None = Field(default=None, max_length=40)
    framework: Literal["CEFR", "LASLLIAM", "mixed", "unknown"] = "unknown"
    oral_comprehension: LanguageLevel = LanguageLevel.UNKNOWN
    oral_production: LanguageLevel = LanguageLevel.UNKNOWN
    reading: LanguageLevel = LanguageLevel.UNKNOWN
    writing: LanguageLevel = LanguageLevel.UNKNOWN
    descriptors: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("l1", "descriptors")
    @classmethod
    def clean_lists(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value if str(item).strip()]
        if any(len(item) > 240 for item in cleaned):
            raise ValueError("learner_profile_text_too_long")
        return cleaned


class LiteracyProfile(StrictModel):
    decoding: float = Field(default=0.5, ge=0.0, le=1.0)
    phonological_awareness: float = Field(default=0.5, ge=0.0, le=1.0)
    grapheme_phoneme: float = Field(default=0.5, ge=0.0, le=1.0)
    orthography: float = Field(default=0.5, ge=0.0, le=1.0)
    morphology: float = Field(default=0.5, ge=0.0, le=1.0)
    syntax: float = Field(default=0.5, ge=0.0, le=1.0)
    functional_literacy: float = Field(default=0.5, ge=0.0, le=1.0)

    @property
    def access_floor(self) -> float:
        return min(self.decoding, self.functional_literacy)


class AccessibilitySupport(StrictModel):
    text_to_speech: bool = False
    speech_to_text: bool = False
    short_lines: bool = False
    line_focus: bool = False
    enlarged_text: bool = False
    low_clutter: bool = False
    synchronized_highlight: bool = False
    alternative_response_modes: bool = False
    reduced_motion: bool = False
    font_scale: float = Field(default=1.0, ge=0.8, le=2.0)
    line_spacing: float = Field(default=1.5, ge=1.0, le=3.0)
    column_chars: int = Field(default=72, ge=28, le=100)


InteractionMode = Literal[
    "text", "voice", "choice", "drag", "diagram", "formula", "code", "image_annotation"
]

SessionPreference = Literal[
    "auto", "micro", "standard", "doposcuola", "exam", "scholar", "literacy_l2"
]


class LearnerProfile(StrictModel):
    age_band: Literal["child", "adolescent", "adult", "unknown"] = "unknown"
    education_level: EducationLevel = EducationLevel.OTHER
    course: str = Field(default="", max_length=160)
    programme: str = Field(default="", max_length=160)
    domain_mastery: dict[str, float] = Field(default_factory=dict)
    prerequisites: dict[str, float] = Field(default_factory=dict)
    language_profile: LanguageProfile = Field(default_factory=LanguageProfile)
    literacy_profile: LiteracyProfile = Field(default_factory=LiteracyProfile)
    accessibility_support: AccessibilitySupport = Field(default_factory=AccessibilitySupport)
    pacing: Literal["slow", "adaptive", "standard", "fast"] = "adaptive"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    fatigue_signal: float = Field(default=0.0, ge=0.0, le=1.0)
    preferred_interaction_modes: list[InteractionMode] = Field(default_factory=lambda: ["text"])
    explicit_user_preferences: dict[str, str | bool | int | float] = Field(default_factory=dict)
    session_preference: SessionPreference = "auto"

    @field_validator("course", "programme")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("domain_mastery", "prerequisites")
    @classmethod
    def bounded_map(cls, value: dict[str, float]) -> dict[str, float]:
        if len(value) > 128:
            raise ValueError("learner_profile_map_too_large")
        cleaned: dict[str, float] = {}
        for key, score in value.items():
            key = str(key).strip()
            if not key or len(key) > 120:
                raise ValueError("learner_profile_invalid_key")
            numeric = float(score)
            if not 0.0 <= numeric <= 1.0:
                raise ValueError("learner_profile_invalid_score")
            cleaned[key] = numeric
        return cleaned

    def compact_context(self) -> dict[str, Any]:
        data = self.model_dump(mode="json")
        data["domain_mastery"] = dict(list(data["domain_mastery"].items())[:24])
        data["prerequisites"] = dict(list(data["prerequisites"].items())[:24])
        return data


def _education_from_school(school_level: str | None) -> EducationLevel:
    text = (school_level or "").casefold()
    if "prim" in text:
        return EducationLevel.PRIMARY
    if "i grado" in text or "middle" in text or "media" in text:
        return EducationLevel.MIDDLE
    if "ii grado" in text or "upper" in text or "superior" in text:
        return EducationLevel.UPPER
    if "univers" in text:
        return EducationLevel.UNIVERSITY
    if "master" in text:
        return EducationLevel.MASTER
    if "post" in text:
        return EducationLevel.POSTGRADUATE
    return EducationLevel.OTHER


def default_learner_profile(
    school_level: str | None = None,
    class_year: str | None = None,
    *,
    override: dict[str, Any] | None = None,
) -> LearnerProfile:
    base: dict[str, Any] = {
        "education_level": _education_from_school(school_level).value,
    }
    if override:
        base.update(override)
    return LearnerProfile.model_validate(base)


def profile_from_student(student: dict[str, Any]) -> LearnerProfile:
    preferences = student.get("preferences") or {}
    raw = preferences.get("learner_profile") if isinstance(preferences, dict) else None
    return default_learner_profile(
        student.get("school_level"),
        student.get("class_year"),
        override=raw if isinstance(raw, dict) else None,
    )
