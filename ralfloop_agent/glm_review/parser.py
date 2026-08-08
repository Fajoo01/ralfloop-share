from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from .models import GlmReview


class ReviewParseError(ValueError):
    pass


INLINE_PROGRESS_RE = re.compile(rb"\r?\n\[t=\d+[^\r\n]*\b(?:tok/fw|tok/s)[^\r\n]*\]\r?\n")


def parse_glm_review(raw: bytes | str, *, max_bytes: int = 131_072) -> GlmReview:
    if isinstance(raw, bytes):
        if len(raw) > max_bytes:
            raise ReviewParseError("output_limit_exceeded")
        raw = INLINE_PROGRESS_RE.sub(b"", raw)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ReviewParseError("output_not_utf8") from exc
    else:
        text = INLINE_PROGRESS_RE.sub(b"", raw.encode("utf-8")).decode("utf-8")
        if len(text.encode("utf-8")) > max_bytes:
            raise ReviewParseError("output_limit_exceeded")
    if not text.strip():
        raise ReviewParseError("output_empty")

    decoder = json.JSONDecoder()
    validation_errors: list[str] = []
    found_object = False
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            continue
        found_object = True
        try:
            return GlmReview.model_validate(value)
        except ValidationError as exc:
            validation_errors.append(_short_validation_error(exc))
    if text.rstrip().endswith(("{", "[", ",", ":")) or text.count("{") > text.count("}"):
        raise ReviewParseError("json_truncated")
    if found_object:
        raise ReviewParseError("schema_invalid:" + ";".join(validation_errors[:3]))
    raise ReviewParseError("json_object_missing")


def _short_validation_error(exc: ValidationError) -> str:
    parts = []
    for row in exc.errors(include_url=False)[:5]:
        location = ".".join(str(item) for item in row.get("loc") or ())
        parts.append(f"{location}:{row.get('type')}")
    return ",".join(parts)


def find_json_objects(text: str) -> list[dict[str, Any]]:
    """Diagnostic helper; never treats an arbitrary object as a valid review."""
    decoder = json.JSONDecoder()
    result = []
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            result.append(value)
    return result
