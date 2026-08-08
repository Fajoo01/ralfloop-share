from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from ralfloop_agent.local_arch.media import AudiobookFactory, SocialMediaFactory, SpeechSegment, VisualRagWorker
from ralfloop_agent.local_arch.policy import RalfPolicy
from ralfloop_agent.local_arch.contracts import CompactRoute
from ralfloop_agent.local_arch.store import ArtifactStore


def test_71_pdf_text_regions(monkeypatch, tmp_path):
    path = tmp_path / "doc.pdf"
    path.write_bytes(b"%PDF-fake")
    fake = subprocess.CompletedProcess([], 0, stdout=b"page one\fpage two", stderr=b"")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    result = VisualRagWorker(tmp_path / "index").ingest(path)
    assert result["pages"] == 2


def test_72_scanned_pdf_low_text(monkeypatch, tmp_path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"%PDF-scan")
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(subprocess, "run", fail)
    result = VisualRagWorker(tmp_path / "index").ingest(path)
    assert result["regions"] == 1


def test_73_page_image_dry_run_hash(tmp_path):
    path = tmp_path / "page.png"
    path.write_bytes(b"png")
    result = VisualRagWorker(tmp_path / "index").ingest(path, dry_run=True)
    assert result["source_hash"] and result["regions"] == 0


@pytest.mark.parametrize("content,question", [
    ("budget\n\n100 euro", "budget"),
    ("tabella entrate uscite", "entrate"),
    ("grafico crescita 20 percento", "crescita"),
])
def test_74_75_table_chart_retrieval(content, question, tmp_path):
    path = tmp_path / "doc.txt"
    path.write_text(content)
    worker = VisualRagWorker(tmp_path / "index")
    document = worker.ingest(path)["document_id"]
    assert worker.query(document, question)["facts"]


def test_76_bounding_box_provenance(tmp_path):
    path = tmp_path / "doc.txt"; path.write_text("uno\n\ndue")
    worker = VisualRagWorker(tmp_path / "index"); document = worker.ingest(path)["document_id"]
    assert len(worker.query(document, "uno")["regions"][0]["bbox"]) == 4


def test_77_page_provenance(tmp_path):
    path = tmp_path / "doc.txt"; path.write_text("one\ftwo")
    worker = VisualRagWorker(tmp_path / "index"); document = worker.ingest(path)["document_id"]
    assert worker.query(document, "two")["regions"][0]["page"] == 2


def test_78_region_retrieval_limit(tmp_path):
    path = tmp_path / "doc.txt"; path.write_text("a\n\nb\n\nc\n\nd")
    worker = VisualRagWorker(tmp_path / "index"); document = worker.ingest(path)["document_id"]
    assert len(worker.query(document, "a b c d", limit=10)["regions"]) == 3


def test_79_no_full_document_resend(tmp_path):
    path = tmp_path / "doc.txt"; path.write_text("short\n\n" + "x" * 5000)
    worker = VisualRagWorker(tmp_path / "index"); document = worker.ingest(path)["document_id"]
    assert worker.query(document, "short")["full_document_sent"] is False


def test_80_low_confidence_empty_scan(monkeypatch, tmp_path):
    path = tmp_path / "scan.pdf"; path.write_bytes(b"pdf")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, a)))
    worker = VisualRagWorker(tmp_path / "index"); document = worker.ingest(path)["document_id"]
    assert worker.query(document, "anything")["confidence"] == 0


def factory(tmp_path):
    return AudiobookFactory(ArtifactStore(tmp_path / "artifacts"), tmp_path / "cache")


def test_81_chapter_segmentation(tmp_path):
    assert len(factory(tmp_path).chapters("Capitolo 1\nA\nCapitolo 2\nB")) == 2


def test_82_dialogue_detection(tmp_path):
    segments = factory(tmp_path).dialogue_segments('“Non verrò”, disse Anna irritata')
    assert any(item.speaker == "anna" for item in segments)


def test_83_narrator_preserved(tmp_path):
    assert factory(tmp_path).dialogue_segments("Testo narrato")[0].speaker == "narrator"


def test_84_unknown_speaker_not_invented(tmp_path):
    assert factory(tmp_path).dialogue_segments('“Ciao”')[0].speaker == "unknown"


def test_85_tts_segment_contract(tmp_path):
    segment = SpeechSegment("anna", "neutral", "normal", 0.5, "Ciao")
    assert segment.text == "Ciao"


def test_86_segment_cache_key_complete(tmp_path):
    segment = SpeechSegment("anna", "neutral", "normal", 0.5, "Ciao")
    left = factory(tmp_path).cache_key(segment, voice="v1", speed=1, model_hash="a", tts_config={})
    right = factory(tmp_path).cache_key(segment, voice="v2", speed=1, model_hash="a", tts_config={})
    assert left != right


def test_87_chapter_assembly_command(tmp_path):
    command = factory(tmp_path).chapter_compose_command(["a.wav", "b.wav"], "chapter.opus")
    assert "concat=n=2" in " ".join(command)


def test_88_loudness_normalization(tmp_path):
    assert "loudnorm" in " ".join(factory(tmp_path).chapter_compose_command(["a.wav"], "chapter.mp3"))


def test_89_metadata_artifact(tmp_path):
    source = tmp_path / "book.txt"; source.write_text("Capitolo 1\nTesto")
    assert factory(tmp_path).plan(source)["artifact"].startswith("sha256:")


def test_90_partial_regeneration_key(tmp_path):
    first = SpeechSegment("narrator", "neutral", "normal", 0.5, "A")
    second = SpeechSegment("narrator", "neutral", "normal", 0.5, "B")
    assert factory(tmp_path).cache_key(first, voice="n", speed=1, model_hash="m", tts_config={}) != factory(tmp_path).cache_key(second, voice="n", speed=1, model_hash="m", tts_config={})


def social(tmp_path):
    return SocialMediaFactory(ArtifactStore(tmp_path / "artifacts"))


def image_spec(fmt="square"):
    return {"format": fmt, "title": "Evento", "verified_facts": ["8 agosto", "Roma", "€ 10"], "logo_ref": "logo.svg"}


def test_91_verified_text_overlay(tmp_path):
    result = social(tmp_path).image(image_spec())
    svg = social(tmp_path).store.get(result["artifact"]).decode()
    assert "8 agosto" in svg and "€ 10" in svg


def test_92_logo_positioning(tmp_path):
    result = social(tmp_path).image(image_spec())
    assert "logo.svg" in social(tmp_path).store.get(result["artifact"]).decode()


@pytest.mark.parametrize("fmt,size", [("square", (1080,1080)), ("story", (1080,1920)), ("portrait", (1080,1350))])
def test_93_95_image_variants(fmt, size, tmp_path):
    result = social(tmp_path).image(image_spec(fmt), dry_run=True)
    assert (result["width"], result["height"]) == size


def video_spec():
    return {"duration_seconds": 15, "shots": [{"keyframe": "a.png", "seconds": 5}, {"keyframe": "b.png", "seconds": 5}, {"keyframe": "c.png", "seconds": 5}], "voiceover_ref": "sha256:" + "a"*64, "subtitles_ref": "sha256:" + "b"*64}


def test_96_video_keyframes(tmp_path):
    result = social(tmp_path).video(video_spec())
    assert result["artifact"].startswith("sha256:")


def test_97_subtitle_generation():
    srt = SocialMediaFactory.subtitles([{"start": 0, "end": 1.5, "text": "Ciao"}])
    assert "00:00:01,500" in srt and "Ciao" in srt


def test_98_tts_voiceover_reference(tmp_path):
    payload = json.loads(social(tmp_path).store.get(social(tmp_path).video(video_spec())["artifact"]))
    assert payload["voiceover_ref"].startswith("sha256:")


def test_99_ffmpeg_compose(tmp_path):
    command = social(tmp_path).compose_command({"inputs": ["a.mp4", "voice.wav"], "output": "out.mp4"})
    assert command[0] == "ffmpeg" and "libx264" in command


def test_100_no_publish_without_approval(tmp_path):
    route = CompactRoute(1, "AP", "external_action", c=1)
    assert social(tmp_path).video(video_spec())["publish"] is False
    assert not RalfPolicy().evaluate(route, operation="social_publish").allowed
