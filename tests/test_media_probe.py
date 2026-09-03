import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ralfloop_agent.unified_assistant.media_probe import FFprobeMediaReader


class Resolver:
    def __init__(self, path): self.path = path
    def resolve_media_path(self, item_id, *, user_id): return self.path


def test_ffprobe_uses_fixed_argv_and_normalizes_metadata_without_path_leak(tmp_path):
    media = tmp_path / "synthetic.mkv"
    media.touch()
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps({
            "streams": [
                {"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "bit_rate": "2000000", "disposition": {"default": 1}},
                {"index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 2, "bit_rate": "128000", "tags": {"title": "Italiano"}},
            ],
            "format": {"duration": "0.5", "format_name": "matroska"},
        }), stderr="")

    probe = FFprobeMediaReader(Resolver(media), allowed_roots=(tmp_path,), runner=runner)
    result = probe.inspect("item-1", user_id="user-1")
    command, kwargs = calls[0]
    assert command[:3] == ["/usr/bin/ffprobe", "-v", "error"]
    assert command[-1] == str(media)
    assert "shell" not in kwargs
    assert {"MISSING_LANGUAGE_TAG", "UNUSUAL_DURATION"} <= set(result.warnings)
    assert str(media) not in result.model_dump_json()


def test_ffprobe_rejects_paths_outside_allowlisted_roots(tmp_path):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.mkv"
    outside.touch()
    probe = FFprobeMediaReader(Resolver(outside), allowed_roots=(allowed,), runner=lambda *_args, **_kwargs: None)
    with pytest.raises(RuntimeError, match="media_path_forbidden"):
        probe.inspect("item-1", user_id="user-1")


def test_ffprobe_fails_closed_on_command_or_payload_failure(tmp_path):
    media = tmp_path / "synthetic.mkv"
    media.touch()
    failed = FFprobeMediaReader(Resolver(media), allowed_roots=(tmp_path,), runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="bad"))
    with pytest.raises(RuntimeError, match="media_probe_failed"):
        failed.inspect("item-1", user_id="user-1")
    malformed = FFprobeMediaReader(Resolver(media), allowed_roots=(tmp_path,), runner=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="not-json", stderr=""))
    with pytest.raises(RuntimeError, match="media_probe_malformed"):
        malformed.inspect("item-1", user_id="user-1")
