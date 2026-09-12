from pathlib import Path
import sys
import time

from ralfloop_agent.teacher.web.fish_tts import FishTTSCache, VOICE_ID


def test_fish_disabled_is_immediate_browser_fallback(tmp_path):
    cache = FishTTSCache(cache_dir=tmp_path, helper=tmp_path / "missing.py")
    assert not cache.enabled
    assert cache.prepare("Ciao") == {"status": "browser_fallback"}
    assert cache.ready_path("Ciao") is None


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
        api_key="secret-for-test",
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


def test_fish_helper_has_no_user_selectable_voice_or_text_argv():
    source = Path("scripts/ralf_teacher_fish_client.py").read_text()
    assert 'VOICE_ID = "peppone"' in source
    assert 'add_argument("--text"' not in source
    assert 'add_argument("--reference' not in source
    assert "TEACHER_FISH_API_KEY" in source
    assert "print(" not in source
