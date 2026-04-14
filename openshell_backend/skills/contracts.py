from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class GrammarEntry(BaseModel):
    token: str
    categoria: str
    lemma: str | None = None
    genere: str | None = None
    numero: str | None = None
    modo: str | None = None
    tempo: str | None = None
    persona: str | None = None


class GrammarOutput(BaseModel):
    items: list[GrammarEntry] = Field(default_factory=list)


class ValidationSummary(BaseModel):
    ok: bool
    reason: str
    details: list[dict] = Field(default_factory=list)
    suggested_target: str | None = None
    suggested_file: str | None = None
