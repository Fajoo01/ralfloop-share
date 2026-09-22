"""Install the validated loopback Fish Speech 1.5 Teacher service."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request


def main() -> int:
    home = Path.home()
    repo = Path(__file__).resolve().parents[1]
    runtime = home / ".local/share/ralf-teacher-voice/fish-speech-1.5"
    python = runtime / ".venv/bin/python"
    model = runtime / "checkpoints/fish-speech-1.5/model.pth"
    decoder = runtime / "checkpoints/fish-speech-1.5/firefly-gan-vq-fsq-8x1024-21hz-generator.pth"
    reference = runtime / "references/peppone/sample.wav"

    required = (python, model, decoder, reference)
    missing = [str(path.relative_to(home)) for path in required if not path.is_file()]
    if missing:
        raise SystemExit("fish15_runtime_missing:" + ",".join(missing))

    units = home / ".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    target = units / "ralf-teacher-fish15.service"
    source = repo / "deploy/systemd/ralf-teacher-fish15.service"
    previous = target.read_bytes() if target.exists() else None
    shutil.copyfile(source, target)
    target.chmod(0o600)

    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "ralf-teacher-fish15.service"],
            check=True,
        )
        request = urllib.request.Request(
            "http://127.0.0.1:19195/v1/health",
            method="POST",
        )
        for attempt in range(90):
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    payload = json.load(response)
                if payload == {"status": "ok"}:
                    break
            except Exception:
                if attempt == 89:
                    raise
                time.sleep(1)
    except Exception:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", "ralf-teacher-fish15.service"],
            check=False,
        )
        if previous is None:
            target.unlink(missing_ok=True)
        else:
            target.write_bytes(previous)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        raise

    print(json.dumps({
        "status": "healthy",
        "service": "ralf-teacher-fish15.service",
        "health": "http://127.0.0.1:19195/v1/health",
        "voice": "peppone",
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
