#!/usr/bin/env python3
"""Internal Fish S2-Pro client for the isolated Teacher web runtime.

Text is read from stdin so it never appears in argv. The voice is deliberately
fixed to the operator-approved persistent reference id ``peppone``. URL and API
key come only from the Teacher service environment and are never printed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

import ormsgpack
import requests

VOICE_ID = "peppone"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    text = sys.stdin.read().strip()
    if not text or len(text) > 600:
        parser.error("bounded_text_required")

    url = os.environ.get("TEACHER_FISH_URL", "").rstrip("/")
    api_key = os.environ.get("TEACHER_FISH_API_KEY", "")
    if not url or not api_key:
        parser.error("fish_configuration_required")
    if args.output.exists():
        parser.error("output_already_exists")

    payload = {
        "text": text,
        "references": [],
        "reference_id": VOICE_ID,
        "format": "wav",
        "latency": "normal",
        "max_new_tokens": 1024,
        "chunk_length": 100,
        "top_p": 0.9,
        "repetition_penalty": 1.1,
        "temperature": 0.7,
        "streaming": False,
        "use_memory_cache": "on",
        "seed": 123,
        "normalize": True,
    }
    response = requests.post(
        f"{url}/v1/tts" if not url.endswith("/v1/tts") else url,
        params={"format": "msgpack"},
        data=ormsgpack.packb(payload),
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/msgpack",
        },
        timeout=(3, 1800),
    )
    response.raise_for_status()
    audio = response.content
    if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
        raise RuntimeError("invalid_wav_response")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(audio)
    os.chmod(args.output, 0o600)


if __name__ == "__main__":
    main()
