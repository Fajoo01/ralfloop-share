#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


def main() -> int:
    spool = Path(os.getenv("BOTTAZZI_AGENDA_CALENDAR_DIR", "/var/lib/ralfloop-bottazzi-agenda/calendar"))
    archive = Path(os.getenv("BOTTAZZI_AGENDA_CALENDAR_ARCHIVE", "/var/lib/ralfloop-bottazzi-agenda/imported"))
    uid = os.getenv("BOTTAZZI_NEXTCLOUD_CALENDAR_UID", "bottazzi-agenda")
    uri = os.getenv("BOTTAZZI_NEXTCLOUD_CALENDAR_URI", "bottazzi-agenda")
    compose = os.getenv("BOTTAZZI_NEXTCLOUD_COMPOSE", "/home/bandi/selfhost/nextcloud/docker-compose.yml")
    archive.mkdir(parents=True, exist_ok=True)
    if not spool.is_dir():
        return 0
    for path in sorted(spool.glob("*.ics")):
        cmd = [
            "docker", "compose", "-f", compose,
            "exec", "-T", "-u", "www-data", "nextcloud",
            "php", "occ", "calendar:import", uid, uri,
            "--errors=1", "--validation=2",
        ]
        with path.open("rb") as src:
            completed = subprocess.run(cmd, stdin=src, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if completed.returncode != 0:
            print(f"IMPORT_FAIL {path.name}")
            return completed.returncode
        shutil.move(str(path), archive / path.name)
        print(f"IMPORTED {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
