"""Audio pipeline without fabricated speech. Browser speech is an optional provider."""
from typing import Protocol
import re


class TTSProvider(Protocol):
    name: str
    def synthesize(self, text: str, index: int) -> dict: ...


class PendingTTS:
    name = "provider_required"
    def synthesize(self, text, index):
        return {"chapter": index, "text": text, "url": None, "status": "provider_required"}


class BrowserTTS:
    """Playable through speechSynthesis where installed; no downloadable audio claimed."""
    name = "browser_speech"
    def synthesize(self, text, index):
        return {"chapter": index, "text": text, "url": None, "status": "browser_voice_required"}


def segment(text, limit=600):
    text = re.sub(r"\s+", " ", text).strip()
    chunks = []
    while text:
        end = min(len(text), limit)
        if end < len(text):
            split = text.rfind(" ", 0, end)
            if split > limit // 2:
                end = split
        chunks.append(text[:end])
        text = text[end:].strip()
    return chunks


def prepare_tracks(chunks, provider: TTSProvider):
    return [provider.synthesize(part, i) for i, part in enumerate(segment(" ".join(chunks)))]
