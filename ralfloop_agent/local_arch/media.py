from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import html
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from .store import ArtifactStore, canonical_json, sha256_bytes


@dataclass(frozen=True)
class VisualRegion:
    document_id: str
    page: int
    region: str
    bbox: tuple[float, float, float, float]
    source_hash: str
    text: str
    model: str
    confidence: float

    def provenance(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "page": self.page,
            "region": self.region,
            "bbox": list(self.bbox),
            "source_hash": self.source_hash,
            "model": self.model,
            "confidence": self.confidence,
        }


class VisualRagWorker:
    """Region-first index. VLM/OCR adapters are optional and on-demand."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.index = self.root / "regions.jsonl"

    def ingest(self, source: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
        path = Path(source)
        data = path.read_bytes()
        digest = sha256_bytes(data)
        document_id = f"doc-{digest[:16]}"
        if dry_run:
            return {"document_id": document_id, "source_hash": digest, "mode": "dry_run", "regions": 0}
        pages = self._extract_pages(path, data)
        self.root.mkdir(parents=True, exist_ok=True)
        records: list[VisualRegion] = []
        for page_number, page_text in enumerate(pages, 1):
            blocks = [block.strip() for block in re.split(r"\n\s*\n", page_text) if block.strip()]
            if not blocks:
                blocks = [""]
            height = 1.0 / len(blocks)
            for index, block in enumerate(blocks):
                records.append(VisualRegion(document_id, page_number, f"r{index + 1}", (0.0, index * height, 1.0, (index + 1) * height), digest, block, "deterministic_text", 1.0 if block else 0.0))
        with self.index.open("a", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(asdict(record), ensure_ascii=False, separators=(",", ":")) + "\n")
        return {"document_id": document_id, "source_hash": digest, "pages": len(pages), "regions": len(records)}

    def query(self, document_id: str, question: str, *, limit: int = 3) -> dict[str, Any]:
        terms = set(re.findall(r"\w+", question.casefold()))
        regions: list[VisualRegion] = []
        for line in self.index.read_text(encoding="utf-8").splitlines():
            raw = json.loads(line)
            if raw["document_id"] == document_id:
                raw["bbox"] = tuple(raw["bbox"])
                regions.append(VisualRegion(**raw))
        scored = sorted(regions, key=lambda item: len(terms & set(re.findall(r"\w+", item.text.casefold()))), reverse=True)[: max(1, min(3, limit))]
        facts = [{"text": item.text[:800], "provenance": item.provenance()} for item in scored if item.text]
        confidence = min((item.confidence for item in scored), default=0.0)
        return {"v": 1, "facts": facts, "regions": [item.provenance() for item in scored], "confidence": confidence, "full_document_sent": False}

    @staticmethod
    def _extract_pages(path: Path, data: bytes) -> list[str]:
        if path.suffix.casefold() == ".pdf":
            try:
                result = subprocess.run(["pdftotext", "-layout", str(path), "-"], check=True, capture_output=True, timeout=30)
                text = result.stdout.decode("utf-8", "replace")
                if text.strip():
                    return text.split("\f")
            except (FileNotFoundError, subprocess.SubprocessError):
                return [""]  # scanned page requires the separately benchmarked visual/OCR adapter
        return data.decode("utf-8", "replace").split("\f")


@dataclass(frozen=True)
class SpeechSegment:
    speaker: str
    emotion: str
    pace: str
    intensity: float
    text: str


class AudiobookFactory:
    def __init__(self, store: ArtifactStore, cache_root: str | Path):
        self.store = store
        self.cache_root = Path(cache_root)

    @staticmethod
    def chapters(text: str) -> list[tuple[str, str]]:
        marker = re.compile(r"(?im)^(?:capitolo|chapter)\s+[^\n]+$")
        matches = list(marker.finditer(text))
        if not matches:
            return [("chapter-1", text.strip())]
        result: list[tuple[str, str]] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            result.append((match.group(0).strip(), text[match.end():end].strip()))
        return result

    @staticmethod
    def dialogue_segments(text: str) -> list[SpeechSegment]:
        pattern = re.compile(r'[«“"]([^»”"]+)[»”"](?:,?\s*disse\s+([A-ZÀ-Ý][\wÀ-ÿ-]*)(?:\s+(irritat[oa]|calm[oa]|triste))?)?', re.IGNORECASE)
        segments: list[SpeechSegment] = []
        cursor = 0
        for match in pattern.finditer(text):
            if match.start() > cursor and text[cursor:match.start()].strip():
                segments.append(SpeechSegment("narrator", "neutral", "normal", 0.5, text[cursor:match.start()].strip()))
            speaker = (match.group(2) or "unknown").casefold()
            emotion = (match.group(3) or "neutral").casefold()
            pace = "fast" if emotion.startswith("irritat") else "normal"
            segments.append(SpeechSegment(speaker, emotion, pace, 0.7 if pace == "fast" else 0.5, match.group(1).strip()))
            cursor = match.end()
        if cursor < len(text) and text[cursor:].strip():
            segments.append(SpeechSegment("narrator", "neutral", "normal", 0.5, text[cursor:].strip()))
        return segments or [SpeechSegment("narrator", "neutral", "normal", 0.5, text.strip())]

    @staticmethod
    def cache_key(segment: SpeechSegment, *, voice: str, speed: float, model_hash: str, tts_config: Mapping[str, Any]) -> str:
        return sha256_bytes(canonical_json({
            "text_hash": sha256_bytes(segment.text.encode()), "voice": voice, "style": segment.emotion,
            "speed": speed, "model_hash": model_hash, "tts_config": dict(tts_config),
        }))

    def plan(self, source: str | Path, *, model_id: str = "kokoro-82m-unbenchmarked", dry_run: bool = False) -> dict[str, Any]:
        text = Path(source).read_text(encoding="utf-8")
        chapters = self.chapters(text)
        planned = []
        for chapter_index, (title, body) in enumerate(chapters, 1):
            segments = self.dialogue_segments(body)
            planned.append({"chapter": chapter_index, "title": title, "segments": [asdict(item) for item in segments]})
        artifact = None
        if not dry_run:
            artifact = self.store.put(canonical_json(planned), media_type="application/json", provenance={"pipeline": "audiobook", "model": model_id}).ref
        return {"v": 1, "status": "planned", "model": model_id, "chapters": len(planned), "artifact": artifact, "synthesis": "not_run"}

    @staticmethod
    def chapter_compose_command(segments: Sequence[str], output: str) -> list[str]:
        if not segments or Path(output).suffix.casefold() not in {".mp3", ".opus", ".m4a"}:
            raise ValueError("audiobook_compose_inputs")
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        for segment in segments:
            command.extend(["-i", segment])
        inputs = "".join(f"[{index}:a]" for index in range(len(segments)))
        command.extend(["-filter_complex", f"{inputs}concat=n={len(segments)}:v=0:a=1,loudnorm=I=-18:TP=-2:LRA=11[a]", "-map", "[a]", output])
        return command


FORMATS = {"square": (1080, 1080), "portrait": (1080, 1350), "story": (1080, 1920), "landscape": (1200, 628)}


class SocialMediaFactory:
    def __init__(self, store: ArtifactStore):
        self.store = store

    def image(self, spec: Mapping[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        fmt = str(spec.get("format", "square"))
        if fmt not in FORMATS:
            raise ValueError("invalid_social_format")
        if not spec.get("verified_facts"):
            raise ValueError("verified_facts_required")
        width, height = FORMATS[fmt]
        title = html.escape(str(spec.get("title", "")))
        critical = [html.escape(str(item)) for item in spec["verified_facts"]]
        logo = html.escape(str(spec.get("logo_ref", "")))
        lines = "".join(f'<text x="64" y="{240 + index * 72}" font-size="44" fill="#fff">{item}</text>' for index, item in enumerate(critical))
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<rect width="100%" height="100%" fill="#17223b"/><text x="64" y="130" font-size="64" font-weight="700" fill="#fff">{title}</text>'
            f'{lines}<text x="64" y="{height - 64}" font-size="24" fill="#9fd3ff">{logo}</text></svg>'
        )
        if dry_run:
            return {"format": fmt, "width": width, "height": height, "artifact": None, "approval_required": False}
        artifact = self.store.put(svg.encode(), media_type="image/svg+xml", provenance={"pipeline": "social_image", "critical_text": "deterministic"})
        return {"format": fmt, "width": width, "height": height, "artifact": artifact.ref, "approval_required": False}

    def video(self, spec: Mapping[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
        duration = int(spec.get("duration_seconds", 15))
        if not 15 <= duration <= 30:
            raise ValueError("social_video_duration")
        shots = spec.get("shots", [])
        if not shots or any(not 3 <= int(item.get("seconds", 0)) <= 5 for item in shots):
            raise ValueError("microclip_duration")
        plan = {"v": 1, "mode": "image_to_video", "duration_seconds": duration, "shots": shots, "voiceover_ref": spec.get("voiceover_ref"), "subtitles_ref": spec.get("subtitles_ref"), "publish": False}
        artifact = None if dry_run else self.store.put(canonical_json(plan), media_type="application/json", provenance={"pipeline": "social_video_storyboard"}).ref
        return {"status": "planned", "artifact": artifact, "publish": False, "approval_required_for_publish": True}

    @staticmethod
    def subtitles(cues: Sequence[Mapping[str, Any]]) -> str:
        rows: list[str] = []
        for index, cue in enumerate(cues, 1):
            start = _srt_time(float(cue["start"]))
            end = _srt_time(float(cue["end"]))
            text = str(cue["text"]).replace("\r", " ").strip()
            rows.extend((str(index), f"{start} --> {end}", text, ""))
        return "\n".join(rows)

    def compose_command(self, spec: Mapping[str, Any]) -> list[str]:
        inputs = spec.get("inputs")
        output = spec.get("output")
        if not isinstance(inputs, list) or not inputs or not isinstance(output, str):
            raise ValueError("compose_inputs_output_required")
        if any(Path(item).suffix.casefold() not in {".mp4", ".mov", ".png", ".jpg", ".wav", ".opus", ".mp3"} for item in inputs):
            raise ValueError("compose_input_type")
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y"]
        for item in inputs:
            command.extend(["-i", item])
        command.extend(["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", output])
        return command


def _srt_time(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    whole, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole:02d},{millis:03d}"
