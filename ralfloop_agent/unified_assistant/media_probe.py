from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from pydantic import Field

from .contracts import StrictModel
from .jellyfin_semantic import JellyfinMediaStream


class MediaPathResolver(Protocol):
    def resolve_media_path(self, item_id: str, *, user_id: str) -> str | Path: ...


class FFprobeInspection(StrictModel):
    item_id: str = Field(min_length=1, max_length=240)
    streams: tuple[JellyfinMediaStream, ...]
    duration_seconds: float | None = Field(default=None, ge=0)
    format_name: str | None = Field(default=None, max_length=120)
    warnings: tuple[str, ...] = ()


class FFprobeMediaReader:
    """Fixed-argument local probe. No shell and no path is returned to callers."""

    def __init__(
        self, resolver: MediaPathResolver, *, allowed_roots: tuple[str | Path, ...],
        binary: str | Path = "/usr/bin/ffprobe", timeout: float = 30,
        runner: Callable[..., Any] = subprocess.run,
    ) -> None:
        self.resolver = resolver
        self.allowed_roots = tuple(Path(root).resolve() for root in allowed_roots)
        self.binary = Path(binary)
        self.timeout = timeout
        self.runner = runner
        if not self.allowed_roots or any(root == Path("/") for root in self.allowed_roots):
            raise ValueError("ffprobe_allowed_roots_invalid")
        if self.binary.name != "ffprobe" or not 1 <= timeout <= 120:
            raise ValueError("ffprobe_configuration_invalid")

    def get_media_streams(self, item_id: str, *, user_id: str) -> tuple[JellyfinMediaStream, ...]:
        return self.inspect(item_id, user_id=user_id).streams

    def inspect(self, item_id: str, *, user_id: str) -> FFprobeInspection:
        try:
            path = Path(self.resolver.resolve_media_path(item_id, user_id=user_id)).resolve(strict=True)
        except (OSError, RuntimeError, ValueError):
            raise RuntimeError("media_path_unavailable") from None
        if not any(path.is_relative_to(root) for root in self.allowed_roots):
            raise RuntimeError("media_path_forbidden")
        command = [
            str(self.binary), "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ]
        try:
            completed = self.runner(command, capture_output=True, text=True, timeout=self.timeout, check=False)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("media_probe_failed") from None
        if completed.returncode != 0 or len(completed.stdout) > 5_000_000:
            raise RuntimeError("media_probe_failed")
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, json.JSONDecodeError):
            raise RuntimeError("media_probe_malformed") from None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("streams"), list):
            raise RuntimeError("media_probe_malformed")
        source_id = "ffprobe-" + hashlib.sha256(item_id.encode()).hexdigest()[:16]
        streams: list[JellyfinMediaStream] = []
        warnings: list[str] = []
        for raw in payload["streams"]:
            if not isinstance(raw, Mapping) or not isinstance(raw.get("index"), int) or not raw.get("codec_type"):
                warnings.append("INVALID_STREAM")
                continue
            tags = raw.get("tags") if isinstance(raw.get("tags"), Mapping) else {}
            disposition = raw.get("disposition") if isinstance(raw.get("disposition"), Mapping) else {}
            streams.append(JellyfinMediaStream(
                media_source_id=source_id, index=raw["index"], type=str(raw["codec_type"]),
                codec=_optional_str(raw.get("codec_name")), language=_optional_str(tags.get("language")),
                display_title=_optional_str(tags.get("title")), is_default=bool(disposition.get("default")),
                channels=_optional_int(raw.get("channels")), width=_optional_int(raw.get("width")),
                height=_optional_int(raw.get("height")), bitrate=_optional_int(raw.get("bit_rate")),
            ))
            if raw.get("codec_type") in {"audio", "subtitle"} and not tags.get("language"):
                warnings.append("MISSING_LANGUAGE_TAG")
        format_row = payload.get("format") if isinstance(payload.get("format"), Mapping) else {}
        duration = _optional_float(format_row.get("duration"))
        if duration is not None and duration < 1:
            warnings.append("UNUSUAL_DURATION")
        if not streams:
            warnings.append("NO_VALID_STREAMS")
        return FFprobeInspection(
            item_id=item_id, streams=tuple(streams), duration_seconds=duration,
            format_name=_optional_str(format_row.get("format_name")), warnings=tuple(dict.fromkeys(warnings)),
        )


def _optional_str(value: Any) -> str | None:
    return str(value)[:120] if value not in (None, "") else None


def _optional_int(value: Any) -> int | None:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


__all__ = ["FFprobeInspection", "FFprobeMediaReader", "MediaPathResolver"]
