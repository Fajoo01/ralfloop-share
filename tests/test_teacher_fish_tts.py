from pathlib import Path
import logging
import sys
import time

from ralfloop_agent.teacher.web.fish_tts import FishTTSCache, VOICE_ID


def test_fish_disabled_is_immediate_browser_fallback(tmp_path):
    cache = FishTTSCache(cache_dir=tmp_path, helper=tmp_path / "missing.py")
    assert not cache.enabled
    assert cache.prepare("Ciao") == {"status": "browser_fallback"}
    assert cache.ready_path("Ciao") is None


def test_fish_disabled_reports_only_missing_configuration_names(tmp_path, caplog):
    helper = tmp_path / "helper.py"
    helper.write_text("pass\n")
    caplog.set_level(logging.WARNING, logger="teacher.web.fish_tts")

    cache = FishTTSCache(
        base_url="https://fish.internal.example",
        api_key="",
        cache_dir=tmp_path / "cache",
        python="",
        helper=helper,
    )

    assert cache.missing_configuration() == (
        "TEACHER_FISH_API_KEY",
        "TEACHER_FISH_PYTHON",
    )
    assert not cache.enabled
    logged = caplog.text
    assert "fish_tts_disabled" in logged
    assert "TEACHER_FISH_API_KEY" in logged
    assert "TEACHER_FISH_PYTHON" in logged
    assert "https://fish.internal.example" not in logged


def test_loopback_fish_does_not_require_api_key(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("pass\n")
    cache = FishTTSCache(
        base_url="http://127.0.0.1:19195",
        api_key="",
        cache_dir=tmp_path / "cache",
        python=sys.executable,
        helper=helper,
    )
    assert cache.missing_configuration() == ()
    assert cache.enabled


def test_non_loopback_fish_without_api_key_is_disabled(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("pass\n")
    cache = FishTTSCache(
        base_url="http://10.0.0.5:19195",
        api_key="",
        cache_dir=tmp_path / "cache",
        python=sys.executable,
        helper=helper,
    )
    assert cache.missing_configuration() == ("TEACHER_FISH_API_KEY",)
    assert not cache.enabled


def test_fish_missing_helper_is_explicit_without_path_leak(tmp_path, caplog):
    missing = tmp_path / "private-helper-location.py"
    caplog.set_level(logging.WARNING, logger="teacher.web.fish_tts")

    cache = FishTTSCache(
        base_url="http://127.0.0.1:8080",
        api_key="secret-for-test",
        cache_dir=tmp_path / "cache",
        python=sys.executable,
        helper=missing,
    )

    assert cache.missing_configuration() == ("TEACHER_FISH_HELPER",)
    assert "TEACHER_FISH_HELPER" in caplog.text
    assert str(missing) not in caplog.text
    assert "secret-for-test" not in caplog.text


def test_fish_cache_is_bounded_async_and_deterministic(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text(
        "import argparse,sys\n"
        "p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args()\n"
        "text=sys.stdin.read();assert text=='Ciao ragazzi.'\n"
        "open(a.output,'wb').write(b'RIFF'+b'0'*4+b'WAVE'+b'0'*40)\n"
    )
    cache = FishTTSCache(
        base_url="http://127.0.0.1:1",
        api_key="",
        cache_dir=tmp_path / "cache",
        python=sys.executable,
        helper=helper,
        queue_size=1,
    )
    assert cache.enabled
    assert VOICE_ID == "peppone"
    first = cache.prepare("  Ciao   ragazzi.  ")
    assert first["status"] == "pending"
    deadline = time.time() + 5
    ready = None
    while time.time() < deadline:
        ready = cache.ready_path("Ciao ragazzi.")
        if ready:
            break
        time.sleep(0.02)
    assert ready is not None
    assert ready.name.endswith(".wav")
    assert cache.prepare("Ciao ragazzi.") == {"status": "ready"}
    assert list((tmp_path / "cache").glob("*.wav")) == [ready]


def test_failed_fish_generation_is_not_requeued_in_a_tight_loop(tmp_path):
    helper = tmp_path / "helper.py"
    helper.write_text("raise SystemExit(7)\n")
    cache = FishTTSCache(
        base_url="http://127.0.0.1:1",
        api_key="",
        cache_dir=tmp_path / "cache",
        python=sys.executable,
        helper=helper,
        queue_size=1,
        failure_backoff_seconds=60,
    )

    assert cache.prepare("Ciao") == {"status": "pending"}
    deadline = time.time() + 5
    status = None
    while time.time() < deadline:
        status = cache.prepare("Ciao")["status"]
        if status == "unavailable":
            break
        time.sleep(0.02)

    assert status == "unavailable"
    assert cache.ready_path("Ciao") is None


def test_fish_helper_has_no_user_selectable_voice_or_text_argv():
    source = Path("scripts/ralf_teacher_fish_client.py").read_text()
    assert 'VOICE_ID = "peppone"' in source
    assert 'add_argument("--text"' not in source
    assert 'add_argument("--reference' not in source
    assert '"latency"' not in source
    assert "TEACHER_FISH_API_KEY" in source
    assert "print(" not in source
