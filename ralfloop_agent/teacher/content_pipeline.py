"""Bounded, provenance-first ingestion for Teacher study materials."""
from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class SourceClass(StrEnum):
    AUTHORITATIVE = "authoritative"
    OER = "oer"
    STUDENT_MATERIAL = "student_material"
    GENERATED = "generated"


class ContentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str = Field(min_length=1, max_length=200)
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=100000)
    source_class: SourceClass
    rights: str = Field(default="", max_length=80)
    license: str = Field(default="", max_length=160)
    locator: str = Field(default="", max_length=300)
    human_reviewed: bool = False

    @model_validator(mode="after")
    def provenance_gate(self):
        if self.source_class in {SourceClass.AUTHORITATIVE, SourceClass.OER} and not self.license:
            raise ValueError("licensed_source_requires_license")
        if self.source_class is SourceClass.STUDENT_MATERIAL and self.rights not in {
            "own", "authorized", "public_domain", "compatible_license"
        }:
            raise ValueError("student_material_rights_required")
        return self


class ContentChunk(BaseModel):
    model_config = ConfigDict(extra="forbid")
    chunk_id: str
    text: str
    topic_ids: list[str] = Field(default_factory=list)


class ContentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_hash: str
    source: dict[str, Any]
    topic_ids: list[str]
    chunks: list[ContentChunk]
    trusted: bool


def segment_text(text: str, *, max_chars: int = 1200) -> list[str]:
    if max_chars < 200 or max_chars > 5000:
        raise ValueError("invalid_chunk_size")
    paragraphs = [" ".join(part.split()) for part in re.split(r"\n\s*\n+", text) if part.strip()]
    chunks: list[str] = []
    for paragraph in paragraphs:
        while len(paragraph) > max_chars:
            cut = paragraph.rfind(". ", 0, max_chars + 1)
            if cut < max_chars // 3:
                cut = paragraph.rfind(" ", 0, max_chars + 1)
            if cut <= 0:
                cut = max_chars
            chunks.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].lstrip(". ")
        if paragraph:
            chunks.append(paragraph)
    return chunks


class ContentPipeline:
    def __init__(self, curriculum):
        self.curriculum = curriculum

    def ingest(self, student: dict[str, Any], source: ContentSource) -> ContentRecord:
        topic_ids = self.curriculum.map_material(student, source.text)
        digest = sha256(source.text.encode("utf-8")).hexdigest()
        chunks = [
            ContentChunk(
                chunk_id=f"{digest[:16]}:{index}",
                text=text,
                topic_ids=list(topic_ids),
            )
            for index, text in enumerate(segment_text(source.text))
        ]
        trusted = (
            source.human_reviewed
            and source.source_class in {SourceClass.AUTHORITATIVE, SourceClass.OER}
            and bool(source.license)
        )
        public_source = source.model_dump(mode="json", exclude={"text"})
        return ContentRecord(
            content_hash=digest,
            source=public_source,
            topic_ids=topic_ids,
            chunks=chunks,
            trusted=trusted,
        )

    @staticmethod
    def evidence_pack(record: ContentRecord, query: str, *, max_chunks: int = 4) -> dict[str, Any]:
        if not 1 <= max_chunks <= 8:
            raise ValueError("invalid_evidence_pack_size")
        terms = {token for token in re.findall(r"\w+", query.casefold()) if len(token) > 2}
        ranked = sorted(
            record.chunks,
            key=lambda chunk: sum(token in chunk.text.casefold() for token in terms),
            reverse=True,
        )[:max_chunks]
        return {
            "content_hash": record.content_hash,
            "source": record.source,
            "trusted": record.trusted,
            "chunks": [chunk.model_dump(mode="json") for chunk in ranked],
        }
