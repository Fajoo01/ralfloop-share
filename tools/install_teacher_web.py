"""Publish a verified Teacher-only web release under the current non-root user.

Does not alter shared production/current, the VPN or existing Teacher services.
Run only after scoped tests. Rollback changes only this web application's symlink.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import sqlite3

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.build_ralfloop_production_release import build_release, publish_current


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name:
            values[name] = value.strip()
    return values


def _ensure_env_value(path: Path, name: str, value: str) -> None:
    """Append a non-secret default without replacing operator configuration."""
    if _read_env_file(path).get(name):
        return
    if "\n" in value or "\r" in value:
        raise SystemExit("invalid_teacher_environment_value")
    existing = path.read_text() if path.exists() else ""
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    with path.open("a") as stream:
        stream.write(f"{prefix}{name}={value}\n")


def _voice_diagnostic(values: dict[str, str], release: Path) -> dict:
    required = ("TEACHER_FISH_URL", "TEACHER_FISH_API_KEY", "TEACHER_FISH_PYTHON")
    missing = [name for name in required if not values.get(name, "").strip()]
    if not (release / "scripts/ralf_teacher_fish_client.py").is_file():
        missing.append("TEACHER_FISH_HELPER")
    return {
        "provider": "fish",
        "voice": "peppone",
        "configured": not missing,
        "missing": missing,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true", help="Start only the user's loopback web service")
    parser.add_argument("--rollback", help="Previously built immutable web release commit")
    args = parser.parse_args()
    os.umask(0o077)
    repo = Path(__file__).resolve().parents[1]
    root = Path.home() / ".local/share/ralf-teacher-web"
    current = root / "current"
    previous = str(current.resolve()) if current.exists() else None
    commit = args.rollback or subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip()
    if len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise SystemExit("invalid_release_commit")
    release = root / "releases" / commit
    if not release.exists():
        if args.rollback: raise SystemExit("rollback_release_not_found")
        build_release(repo, commit, root / "releases")
    manifest = release / "MANIFEST.sha256"
    for line in manifest.read_text().splitlines():
        expected, relative = line.split("  ",1)
        target = (release / relative).resolve()
        if not target.is_relative_to(release.resolve()) or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
            raise SystemExit("release_integrity_failed")
    if not args.install:
        print(json.dumps({"status":"prepared","release":str(release),"port":19139,"binding":"127.0.0.1"}))
        return
    config = Path.home() / ".config/ralf-teacher-web.env"
    config.parent.mkdir(parents=True,exist_ok=True)
    db = Path.home() / ".local/state/ralf-teacher-web/student.sqlite3"
    if not config.exists():
        config.write_text(f"TEACHER_WEB_DB={db}\nTEACHER_WEB_ORIGIN=http://127.0.0.1:19139\n")
    # The installer is already run with the approved Teacher virtualenv. Reuse
    # that interpreter for the bounded Fish helper without storing any secret.
    _ensure_env_value(config, "TEACHER_FISH_PYTHON", sys.executable)
    config.chmod(0o600)
    values = _read_env_file(config)
    db = Path(values.get("TEACHER_WEB_DB", str(db)))
    origin = values.get("TEACHER_WEB_ORIGIN", "http://127.0.0.1:19139")
    health_host = urllib.parse.urlsplit(origin).netloc
    if not health_host:
        raise SystemExit("invalid_teacher_web_origin")
    voice = _voice_diagnostic(values, release)
    if db.exists():
        # SQLite backup API preserves WAL data; no destructive migrations.
        with sqlite3.connect(db) as source, sqlite3.connect(str(db) + f".backup-{time.time_ns()}") as backup:
            source.backup(backup)
    units = Path.home() / ".config/systemd/user"
    units.mkdir(parents=True,exist_ok=True)
    unit = units / "ralf-teacher-web.service"
    previous_unit = unit.read_bytes() if unit.exists() else None
    unit.write_bytes((release / "deploy/systemd/ralf-teacher-web.service").read_bytes())
    publish_current(release,current)
    try:
        subprocess.run(["systemctl","--user","daemon-reload"],check=True)
        subprocess.run(["systemctl","--user","enable","--now","ralf-teacher-web.service"],check=True)
        subprocess.run(["systemctl","--user","restart","ralf-teacher-web.service"],check=True)
        health_request = urllib.request.Request("http://127.0.0.1:19139/health", headers={"Host": health_host})
        for attempt in range(20):
            try:
                with urllib.request.urlopen(health_request,timeout=5) as response:
                    if json.load(response) == {"status":"ok"}: break
            except Exception:
                if attempt == 19: raise
                time.sleep(.5)
    except Exception:
        if previous:
            publish_current(previous,current)
            if previous_unit is not None:
                unit.write_bytes(previous_unit)
                subprocess.run(["systemctl","--user","daemon-reload"],check=False)
            subprocess.run(["systemctl","--user","restart","ralf-teacher-web.service"],check=False)
        else:
            subprocess.run(["systemctl","--user","disable","--now","ralf-teacher-web.service"],check=False)
        raise
    print(json.dumps({"status":"healthy","release":str(release),"previous":previous,"health":"http://127.0.0.1:19139/health","voice":voice}))


if __name__ == "__main__": main()
