"""Versioned curriculum, transparent adaptation and controlled activity contracts."""
from __future__ import annotations

import json
import math
import re
from fractions import Fraction
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KINDS = ("multiple_choice", "true_false", "free_answer", "matching", "grouping",
         "ordering", "fill_blank", "flashcards", "memory", "definition_match",
         "sequence", "timed_challenge", "guided_exercise", "simulation")
ERRORS = ("distraction", "calculation", "conceptual", "missing_prerequisite",
          "instruction", "incomplete", "nearly_correct")


class ActivityContent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    activity_type: Literal["multiple_choice", "true_false", "free_answer", "matching",
                           "grouping", "ordering", "fill_blank", "flashcards", "memory",
                           "definition_match", "sequence", "timed_challenge",
                           "guided_exercise", "simulation"]
    instructions: str = Field(min_length=3, max_length=2000)
    items: list[str] = Field(default_factory=list, max_length=12)
    choices: list[str] = Field(default_factory=list, max_length=12)
    answer: str | list[str] = Field(default="")
    hints: list[str] = Field(default_factory=list, max_length=3)
    evaluation_rule: Literal["exact", "semantic", "pairs", "ordered", "reflection"] = "exact"

    @field_validator("instructions", "items", "choices", "answer", "hints")
    @classmethod
    def plain_text(cls, value):
        for part in value if isinstance(value, list) else [value]:
            if len(part) > 2000 or any(c in part for c in "<>\x00"):
                raise ValueError("unsafe_activity_text")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.evaluation_rule == "exact" and (not isinstance(self.answer, str) or not self.answer.strip()):
            raise ValueError("exact_answer_required")
        if self.activity_type in ("multiple_choice", "true_false", "timed_challenge"):
            if self.evaluation_rule != "exact":
                raise ValueError("choice_requires_deterministic_evaluation")
            if len(self.choices) < 2 or len(set(self.choices)) != len(self.choices) or self.answer not in self.choices:
                raise ValueError("invalid_choices")
        if self.activity_type in ("matching", "grouping", "definition_match", "memory", "flashcards"):
            if not self.items or len(self.items) != len(self.answer) or not isinstance(self.answer, list):
                raise ValueError("invalid_pairs")
            if self.evaluation_rule != "pairs" or any(x not in self.choices for x in self.answer):
                raise ValueError("invalid_pair_choices")
        if self.activity_type in ("ordering", "sequence"):
            if self.evaluation_rule != "ordered" or not self.items or not isinstance(self.answer, list) or sorted(self.items) != sorted(self.answer):
                raise ValueError("invalid_order")
        if self.activity_type == "simulation" and self.evaluation_rule != "reflection":
            raise ValueError("invalid_simulation_rule")
        if self.activity_type not in ("simulation",) and self.evaluation_rule == "reflection":
            raise ValueError("invalid_reflection")
        return self


class Curriculum:
    def __init__(self, path: Path | None = None):
        self.data = json.loads((path or Path(__file__).with_name("curriculum.json")).read_text())
        self.topics = {t["id"]: t for t in self.data["topics"]}
        if len(self.topics) != len(self.data["topics"]):
            raise ValueError("duplicate_topic")
        for topic in self.topics.values():
            if any(p not in self.topics for p in topic["prerequisites"]):
                raise ValueError("unknown_prerequisite")

    def available(self, student):
        return [t for t in self.topics.values()
                if student["grade"] in t["school_levels"].get(student["school_level"], [])
                and (not t["school_tracks"] or student["school_track"] in t["school_tracks"])]

    def require(self, student, topic):
        matches = [t for t in self.available(student) if t["id"] == topic]
        if not matches:
            raise ValueError("topic_unavailable_for_profile")
        return matches[0]

    def map_material(self, student, text):
        matches = [t["id"] for t in self.available(student)
                   if t["keywords"] and any(k in text.casefold() for k in t["keywords"])]
        if not matches and student.get("school_level") in {"university", "postgraduate", "master"}:
            if "academic_study" in {t["id"] for t in self.available(student)}:
                matches.append("academic_study")
        return matches


def level(xp):
    return 1 + math.isqrt(max(0, xp) // 50)


def mastery_status(score, attempts, due, now):
    if not attempts:
        return "non_visto"
    if due and due <= now:
        return "da_ripassare"
    return "acquisito" if score >= 80 else "da_consolidare" if score >= 50 else "in_apprendimento"


def update_evidence(score, correct, error, first_attempt):
    # Retries carry less evidence. Distraction has a small, explicit cost.
    delta = (20 if first_attempt else 10) if correct else (-3 if error == "distraction" else -10)
    return max(0, min(100, score + delta))


def xp_points(first_attempt, recovered, kind):
    return 10 + (3 if first_attempt else 0) + (2 if recovered else 0) + (2 if kind == "multiple_choice" else 0)


def choose_activity(topic, states, recent, now):
    state = states.get(topic["id"], {})
    score = state.get("score", 0)
    difficulty = min(5, max(1, 1 + score // 25))
    errors = [r["error"] for r in recent[-2:] if not r["correct"]]
    if "missing_prerequisite" in errors:
        for pre in topic["prerequisites"]:
            if states.get(pre, {}).get("score", 0) < 50:
                return {"topic": pre, "activity_type": "guided_exercise", "reason": "prerequisite", "difficulty": 1, "return_topic": topic["id"]}
    if state.get("due", 0) and state["due"] <= now:
        return {"topic": topic["id"], "activity_type": "flashcards", "reason": "spaced_review", "difficulty": difficulty}
    if errors == ["conceptual", "conceptual"]:
        kind, reason = "guided_exercise", "alternative_explanation"
    elif errors:
        kind, reason = "guided_exercise", "retry_with_hint"
    elif score == 0 and topic["id"] in ("fractions", "motion") and not state.get("attempts"):
        kind, reason = "simulation", "visual_diagnostic"
    elif score < 40:
        kind, reason = "matching", "connect_representations"
    elif score < 60:
        kind, reason = "guided_exercise", "reduce_scaffolding"
    elif score < 80:
        kind, reason = "free_answer", "independent_practice"
    else:
        kind, reason = "multiple_choice", "consolidation"
    return {"topic": topic["id"], "activity_type": kind, "reason": reason, "difficulty": difficulty}


def native_content(topic, kind, variant=0):
    """Original author-owned seeds. Content and presentation remain independent."""
    if topic == "motion":
        items, answers = ["2 m/s per 3 s", "3 m/s per 4 s", "1 m/s per 5 s"], ["6 m", "12 m", "5 m"]
        question, answer, choices = "Un corpo viaggia a 2 m/s per 3 s. Quanti metri percorre?", "6", ["6", "5", "1"]
        hint = "La distanza è velocità moltiplicata per tempo."
    elif topic == "water":
        items, answers = ["Ghiaccio", "Acqua nel bicchiere", "Vapore acqueo"], ["Solido", "Liquido", "Gas"]
        question, answer, choices = "Qual è lo stato fisico del ghiaccio?", "Solido", ["Solido", "Liquido", "Gas"]
        hint = "Il ghiaccio mantiene una propria forma."
    elif topic == "equal_parts":
        items, answers = ["Una di due parti uguali", "Una di quattro parti uguali", "Una di tre parti uguali"], ["Metà", "Quarto", "Terzo"]
        question, answer, choices = "Divido un intero in due parti uguali. Come si chiama ciascuna parte?", "Metà", ["Metà", "Quarto", "Terzo"]
        hint = "Conta le parti uguali in cui è diviso l'intero."
    else:
        items, answers = ["1/2", "1/3", "3/4"], ["2/4", "2/6", "6/8"]
        n = variant % 3 + 1
        question, answer, choices = f"Completa la frazione equivalente: {n}/4 = ?/8", str(n * 2), [str(n * 2), str(n), str(n + 5)]
        hint = "Il denominatore raddoppia: applica la stessa operazione al numeratore."
    value = dict(activity_type=kind, instructions=question, answer=answer, choices=[], hints=[hint])
    if kind in ("matching", "grouping", "definition_match", "memory", "flashcards"):
        value.update(instructions="Associa ogni elemento alla rappresentazione corrispondente.", items=items, choices=answers[::-1], answer=answers, evaluation_rule="pairs")
    elif kind in ("ordering", "sequence"):
        ordered = ["1/4", "1/2", "3/4"] if topic == "fractions" else answers
        value.update(instructions="Ordina dal più piccolo al più grande." if topic == "fractions" else "Riordina le risposte seguendo questa sequenza: " + ", ".join(items), items=ordered[::-1], answer=ordered, evaluation_rule="ordered")
    elif kind == "true_false":
        value.update(instructions=question + " Risposta proposta: " + answer + ". Vero o falso?", choices=["Vero", "Falso"], answer="Vero")
    elif kind in ("multiple_choice", "timed_challenge"):
        value["choices"] = choices
    elif kind == "simulation":
        value.update(instructions="Prevedi, modifica, osserva. Poi spiega il risultato.", answer="", evaluation_rule="reflection")
    return ActivityContent.model_validate(value)


def evaluate(content, answer):
    expected = content["answer"]
    if isinstance(expected, list):
        return isinstance(answer, list) and answer == expected
    return isinstance(answer, str) and answer.strip().casefold() == expected.strip().casefold()


def fraction_evidence(instructions, answer):
    """Conservative exact arithmetic guard, not a general symbolic evaluator.

    Recognizes only a single explicit fraction addition/subtraction question,
    or a single missing numerator equality. Returns None on ambiguity, zero
    denominators or non-numeric student response. No eval or code interpretation.
    """
    if not isinstance(answer, str):
        return None
    submitted = re.match(r"^\s*(-?\d{1,6}(?:/\d{1,6})?)(?=\s|[,;.]|$)", answer)
    if not submitted:
        return None
    # Decimal responses require their own parser; never truncate 1.5 to 1.
    if re.match(r"^\s*-?\d+[.,]\d", answer):
        return None
    operations = re.findall(r"(?<![\d/])(\d{1,6}/\d{1,6})\s*([+−-])\s*(\d{1,6}/\d{1,6})(?![\d/])", instructions)
    missing = re.findall(r"(?<![\d/])(\d{1,6})/(\d{1,6})\s*=\s*\?/(\d{1,6})(?!\d)", instructions)
    if len(operations) + len(missing) != 1:
        return None
    try:
        value = Fraction(submitted.group(1))
        if operations:
            left, operator, right = operations[0]
            expected = Fraction(left) + Fraction(right) if operator == "+" else Fraction(left) - Fraction(right)
            explanation = f"{left} {operator} {right} = {expected}. Usa un denominatore comune e opera sui numeratori."
        else:
            numerator, denominator, new_denominator = map(int, missing[0])
            expected = Fraction(numerator, denominator) * new_denominator
            explanation = "Applica lo stesso fattore a numeratore e denominatore."
    except (ValueError, ZeroDivisionError):
        return None
    correct = value == expected
    return {"correct": correct, "feedback": ("Il risultato numerico è corretto. " + explanation if correct else "Controlla il calcolo: porta le frazioni allo stesso denominatore, poi opera sui numeratori."), "error_type": None if correct else "calculation"}


def observe(topic, variables):
    if topic == "fractions":
        if set(variables) != {"numerator", "denominator"}:
            raise ValueError("invalid_variables")
        n, d = variables["numerator"], variables["denominator"]
        if type(n) is not int or type(d) is not int or not 1 <= d <= 12 or not 0 <= n <= d:
            raise ValueError("invalid_ranges")
        return {"value": n / d, "label": f"{n}/{d} = {n / d:g}", "question": "Che cosa rappresentano numeratore e denominatore?"}
    if topic == "motion":
        if set(variables) != {"speed", "time"}:
            raise ValueError("invalid_variables")
        v, t = variables["speed"], variables["time"]
        if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 20 for x in (v, t)):
            raise ValueError("invalid_ranges")
        return {"value": v * t, "label": f"Distanza: {v * t:g} m", "question": "A tempo costante, perché raddoppiare la velocità raddoppia la distanza?"}
    raise ValueError("simulation_unavailable")
