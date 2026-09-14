"""Structured, reviewable teaching content and deterministic curriculum graph."""
from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReviewStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    HUMAN_REVIEWED = "human_reviewed"
    REJECTED = "rejected"


class SourceRef(StrictModel):
    source_id: str = Field(min_length=1, max_length=200)
    title: str = Field(default="", max_length=300)
    locator: str = Field(default="", max_length=300)
    source_class: Literal["authoritative", "oer", "student_material", "generated"]


class HintStep(StrictModel):
    level: int = Field(ge=0, le=4)
    kind: Literal["orientation", "prerequisite", "analogy", "first_step", "near_complete"]
    text: str = Field(min_length=1, max_length=1200)


class KnowledgeUnit(StrictModel):
    concept_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    level_range: list[str] = Field(default_factory=list, max_length=20)
    prerequisites: list[str] = Field(default_factory=list, max_length=24)
    learning_objectives: list[str] = Field(default_factory=list, max_length=24)
    canonical_explanation: dict[str, str] = Field(default_factory=dict)
    examples: list[str] = Field(default_factory=list, max_length=24)
    counterexamples: list[str] = Field(default_factory=list, max_length=24)
    common_misconceptions: list[str] = Field(default_factory=list, max_length=24)
    worked_examples: list[str] = Field(default_factory=list, max_length=24)
    hint_ladder: list[HintStep] = Field(default_factory=list, max_length=5)
    retrieval_questions: list[str] = Field(default_factory=list, max_length=24)
    assessment_items: list[dict] = Field(default_factory=list, max_length=40)
    visual_assets: list[str] = Field(default_factory=list, max_length=24)
    audio_script: list[str] = Field(default_factory=list, max_length=24)
    vocabulary: list[str] = Field(default_factory=list, max_length=60)
    source_refs: list[SourceRef] = Field(default_factory=list, max_length=24)
    license: str = Field(default="", max_length=160)
    review_status: ReviewStatus = ReviewStatus.DRAFT

    @model_validator(mode="after")
    def valid_hint_ladder(self):
        if self.hint_ladder:
            levels = [step.level for step in self.hint_ladder]
            if levels != sorted(set(levels)):
                raise ValueError("hint_ladder_levels_must_be_unique_and_sorted")
        if self.review_status in {ReviewStatus.REVIEWED, ReviewStatus.HUMAN_REVIEWED}:
            if not self.source_refs or not self.license or not self.learning_objectives:
                raise ValueError("trusted_content_requires_provenance")
        return self


class CurriculumEdge(StrictModel):
    source: str
    relation: Literal["prerequisite", "part_of", "example_of", "misconception", "application", "vocabulary", "next_concept"]
    target: str


class CurriculumGraph:
    """Small deterministic graph. No generated/GraphRAG edges are trusted implicitly."""

    def __init__(self, units: list[KnowledgeUnit], edges: list[CurriculumEdge] | None = None):
        self.units = {unit.concept_id: unit for unit in units}
        if len(self.units) != len(units):
            raise ValueError("duplicate_concept_id")
        derived = [
            CurriculumEdge(source=pre, relation="prerequisite", target=unit.concept_id)
            for unit in units for pre in unit.prerequisites
        ]
        self.edges = list(edges or []) + derived
        for edge in self.edges:
            if edge.source not in self.units or edge.target not in self.units:
                raise ValueError("curriculum_edge_unknown_concept")
        self._assert_acyclic_prerequisites()

    def _assert_acyclic_prerequisites(self) -> None:
        dependencies: dict[str, set[str]] = {key: set() for key in self.units}
        for edge in self.edges:
            if edge.relation == "prerequisite":
                dependencies[edge.target].add(edge.source)
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(node: str) -> None:
            if node in done:
                return
            if node in visiting:
                raise ValueError("curriculum_prerequisite_cycle")
            visiting.add(node)
            for parent in dependencies[node]:
                visit(parent)
            visiting.remove(node)
            done.add(node)

        for node in dependencies:
            visit(node)

    def prerequisites_for(self, concept_id: str, *, recursive: bool = False) -> list[str]:
        if concept_id not in self.units:
            raise KeyError("unknown_concept")
        direct = [e.source for e in self.edges if e.relation == "prerequisite" and e.target == concept_id]
        if not recursive:
            return direct
        seen: set[str] = set()
        stack = list(direct)
        while stack:
            item = stack.pop()
            if item in seen:
                continue
            seen.add(item)
            stack.extend(self.prerequisites_for(item))
        return sorted(seen)

    def next_concepts(self, concept_id: str) -> list[str]:
        return [e.target for e in self.edges if e.source == concept_id and e.relation in {"next_concept", "prerequisite"}]


def knowledge_unit_from_curriculum(topic: dict, *, source_id: str = "curriculum.json") -> KnowledgeUnit:
    concept = str(topic["id"])
    objective = [str(item) for item in topic.get("learning_objectives", [])]
    return KnowledgeUnit(
        concept_id=concept,
        title=str(topic["title"]),
        level_range=[str(level) for level in topic.get("school_levels", {})],
        prerequisites=[str(item) for item in topic.get("prerequisites", [])],
        learning_objectives=objective,
        common_misconceptions=[str(item) for item in topic.get("common_misconceptions", [])[:24]],
        hint_ladder=[HintStep.model_validate(item) for item in topic.get("hint_ladder", [])[:5]],
        vocabulary=[str(item) for item in topic.get("keywords", [])[:20]],
        source_refs=[SourceRef(source_id=source_id, title="Bot-tazzi versioned curriculum", source_class="authoritative")],
        license="internal_author_owned",
        review_status=ReviewStatus.HUMAN_REVIEWED,
    )
